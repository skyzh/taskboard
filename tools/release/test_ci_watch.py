"""Exercise local CI and scratch integration with a fictional Git repository."""

from pathlib import Path
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from admit_review import enqueue_admitted
from ci_watch import assemble_and_check, check_commit, git, resolve_commit
from local_queue import drain_queue
from release_plan import ReleaseRejected, verify_manifest
from test_queue_provenance import review, row, submission


class LocalCITest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="taskboard-ci-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Fixture CI")
        git(self.repo, "config", "user.email", "ci@example.test")
        self.write("test_sample.py", """from pathlib import Path
import unittest
class Example(unittest.TestCase):
    def test_value(self):
        self.assertTrue(Path('web/dist/index.html').is_file())
""")
        self.write("tasks", "#!/bin/sh\nexit 0\n")
        (self.repo / "tasks").chmod(0o755)
        self.write("web/package.json", json.dumps({
            "name": "taskboard-ci-fixture", "version": "1.0.0", "scripts": {"build": "node build.mjs"},
        }))
        self.write("web/package-lock.json", json.dumps({
            "name": "taskboard-ci-fixture", "version": "1.0.0", "lockfileVersion": 3,
            "requires": True, "packages": {"": {"name": "taskboard-ci-fixture", "version": "1.0.0"}},
        }))
        self.write("web/build.mjs", """import {mkdirSync, readFileSync, writeFileSync} from 'node:fs'
mkdirSync('dist', {recursive:true})
writeFileSync('dist/index.html', readFileSync('src/title.txt'))
""")
        self.write("web/src/smoke.test.mjs", """import {test} from 'node:test'
import assert from 'node:assert/strict'
test('check fixture', () => assert.ok(true))
""")
        self.write("web/src/title.txt", "Taskboard CI fixture")
        self.commit("Use fictional test files")

    def write(self, relative, data):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def commit(self, message):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", message)
        return resolve_commit(self.repo, "HEAD")

    def check(self):
        return check_commit(self.repo, self.state, "refs/heads/main", sys.executable)

    def test_checks_real_commits_and_inherits_only_unchanged_backend(self):
        first = self.check()
        self.assertEqual(first["status"], "pass")
        self.assertEqual(first["checks"]["python"]["status"], "pass")
        files = verify_manifest(self.state / "artifacts" / first["candidate"], first["candidate"])
        self.assertIn(Path("web/dist/index.html"), files)
        self.assertNotIn(Path("web/node_modules"), files)
        self.assertTrue(os.access(self.state / "artifacts" / first["candidate"] / "tasks", os.X_OK))

        self.write("web/src/title.txt", "Second fixture release")
        self.commit("Edit the web source")
        second = self.check()
        self.assertEqual(second["status"], "pass")
        self.assertEqual(second["checks"]["python"], {"status": "inherited", "commit": first["commit"]})
        self.assertNotEqual(first["candidate"], second["candidate"])
        self.assertEqual(second, self.check())

        self.write("test_sample.py", "raise RuntimeError('Intentional fixture failure')")
        self.commit("Break an actual backend check")
        failed = self.check()
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["checks"]["python"]["status"], "fail")
        self.assertNotIn("candidate", failed)
        self.assertEqual(json.loads((self.state / "last-passing.json").read_text())["commit"], second["commit"])

    def test_owner_and_reviewer_pin_release_two_changelog_to_real_git_range(self):
        previous = self.check()
        self.write("web/src/title.txt", "Fictional active release")
        base = self.commit("Update the active fixture")
        active = self.check()["candidate"]
        self.write("web/src/title.txt", "Fictional release two")
        head = self.commit("An uninformative commit message")
        checked = self.check()
        candidate = checked["candidate"]
        active_manifest = self.state / "artifacts" / active / "SHA256SUMS"
        queue = self.root / "queue"
        entry = {"ticket": "T-169", "summary": "Keep your place when filters change.",
                 "merge_commit": head, "merge_receipt_id": 102}
        notes = {"base_commit": base, "head_commit": head, "entries": [entry]}

        def admit(changelog=notes, *, reviewed=notes, squash=head, receipt_task=169, canonical=True):
            exact = {"commit": head, "candidate": candidate, "base": active}
            events = {
                100: row(100, 78, "fixture-owner", submission(ticket="T-169", changelog=changelog, **exact)),
                101: row(101, 169, "fixture-reviewer", review(changelog=reviewed, **exact)),
                102: row(102, receipt_task, "fixture-owner", {
                    "schema": "taskboard-merge/v1", "action": "merged", "candidate": "e" * 40,
                    "target": "main", "squash": squash,
                }),
            }
            if canonical:
                events[102]["recorded_merge_commit"] = squash

            def invoke(action, params):
                if action == "task":
                    return {"task": {"id": 169, "dri": "fixture-owner"}}
                return {"items": [events[params["before"] - 1]]}

            return enqueue_admitted(invoke, self.state, queue, active_manifest, head, 100, 101,
                                    ["fixture-reviewer"], "T-78", repo=self.repo)

        modified = {**notes, "entries": [{**entry, "summary": "A different changelog."}]}
        outside = {**notes, "entries": [{**entry, "merge_commit": base}]}
        too_old = {**notes, "base_commit": previous["commit"]}
        duplicate = {**notes, "entries": [entry, {**entry, "merge_receipt_id": 103}]}
        for test, error in (
            (lambda: admit(reviewed=modified), "same changelog"),
            (lambda: admit(squash=base), "recorded merge"),
            (lambda: admit(canonical=False), "recorded merge"),
            (lambda: admit(receipt_task=170), "expected task"),
            (lambda: admit(duplicate, reviewed=duplicate), "once per changelog ticket"),
            (lambda: admit(outside, reviewed=outside, squash=base), "inside the selected Git range"),
            (lambda: admit(too_old, reviewed=too_old), "active release's passing CI receipt"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                test()
        self.assertFalse(queue.exists())

        job = admit()
        self.assertEqual(job["changelog"], notes)
        self.assertEqual(admit(), job)
        failed = {**job, "release_id": "z-rejected", "ticket": "T-170"}
        (queue / "z-rejected.json").write_text(json.dumps(failed), encoding="utf-8")

        def verify(_artifact, _job):
            return [{"schema_version": 1, "status": "pass", "app_sha256": candidate,
                     "checks": [{"status": "pass"}]}]

        def apply(_target, _artifact, selected, _restart, _healthy, _journal):
            if selected["ticket"] == "T-170":
                raise ReleaseRejected("Fixture rejected this deployment.")
            return {"ticket": selected["ticket"], "candidate": candidate, "active": candidate, "status": "released"}

        published = []
        outcome, rejection = drain_queue(queue, self.root, self.state / "artifacts", verify, None, None,
                                          published.append, apply)
        self.assertEqual(published, [outcome, rejection])
        self.assertEqual(outcome["changelog"], notes)
        self.assertNotIn("changelog", rejection)
        self.assertEqual(drain_queue(queue, self.root, self.state / "artifacts", verify, None, None,
                                     published.append, apply), [])
        output = subprocess.check_output([
            sys.executable, "-B", Path(__file__).with_name("publish_receipt.py"), "--ledger-receipt",
            "--receipt-url", "https://example.test/tasks/#event-999-log",
            queue / "results" / f"{job['release_id']}.json",
        ])
        self.assertEqual(json.loads(output), {
            "release_id": job["release_id"], "ticket": "T-169", "candidate": candidate,
            "status": "released", "active": candidate, "at": outcome["at"], "changelog": notes,
            "receipt_url": "https://example.test/tasks/#event-999-log",
        })

    def test_independent_siblings_are_checked_together_without_changing_main(self):
        base = self.check()["commit"]
        git(self.repo, "checkout", "-b", "sidebar")
        self.write("web/src/sidebar.txt", "Fixture sidebar")
        sidebar = self.commit("Add the independent sidebar")
        git(self.repo, "checkout", "main")
        git(self.repo, "checkout", "-b", "footer")
        self.write("web/src/footer.txt", "Fixture footer")
        footer = self.commit("Add the independent footer")
        git(self.repo, "checkout", "main")

        merged = assemble_and_check(self.repo, self.state, "refs/heads/main", [sidebar, footer], sys.executable)
        self.assertEqual((merged["status"], merged["base"], merged["inputs"]), ("pass", base, [sidebar, footer]))
        artifact = self.state / "artifacts" / merged["candidate"]
        self.assertTrue((artifact / "web/src/sidebar.txt").is_file())
        self.assertTrue((artifact / "web/src/footer.txt").is_file())
        self.assertEqual(resolve_commit(self.repo, "main"), base)
        self.assertFalse((self.repo / "web/src/sidebar.txt").exists())
        self.assertEqual(json.loads((self.state / "last-passing.json").read_text())["commit"], base)

    def test_conflicting_siblings_leave_the_checked_branch_untouched(self):
        base = resolve_commit(self.repo, "main")
        commits = []
        for label in ("left", "right"):
            git(self.repo, "checkout", "main")
            git(self.repo, "checkout", "-b", label)
            self.write("web/src/title.txt", label)
            commits.append(self.commit("Edit the same fixture line"))
        git(self.repo, "checkout", "main")

        result = assemble_and_check(self.repo, self.state, "refs/heads/main", commits, sys.executable)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflicting_commit"], commits[-1])
        self.assertEqual(resolve_commit(self.repo, "main"), base)
        self.assertEqual((self.repo / "web/src/title.txt").read_text(), "Taskboard CI fixture")


if __name__ == "__main__":
    unittest.main()
