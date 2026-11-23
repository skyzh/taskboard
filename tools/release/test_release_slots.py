"""Rehearse the release controller with a disposable ledger and port selector."""

from contextlib import closing
from pathlib import Path
import json
import sqlite3
from subprocess import CompletedProcess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from release_plan import (
    MountedCheckFailed, ReleaseLayout, file_hash, run_release, switch_current, write_json,
)
from release_slots import ReleaseSlots


PRIOR_PORT = 4397
CANDIDATE_PORT = 4398


class ReleaseSlotsTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name).resolve()
        self.layout = ReleaseLayout(
            root / "current", root / "releases", root / "tasks.sqlite3", root / "backups", root / "lock",
        )
        self.layout.releases.mkdir()
        self.layout.backups.mkdir()
        self.base = self.make_artifact(root / "base", "prior")
        self.previous = self.layout.releases / self.base
        (root / "base").rename(self.previous)
        self.layout.current.symlink_to(self.previous)
        self.artifact = root / "artifact"
        self.digest = self.make_artifact(self.artifact, "candidate")
        self.candidate = self.layout.releases / self.digest
        self.journal = root / "job.activation"
        self.entry = {
            "ticket": "T-1149",
            "author": "Fixture author",
            "base": self.base,
            "candidate": self.digest,
            "checks": [{"candidate": self.digest, "outcome": "pass"}],
            "review": {"candidate": self.digest, "outcome": "pass", "reviewer": "Fixture reviewer"},
            "execution": {"checks": ["fixture"]},
        }
        with closing(sqlite3.connect(self.layout.database)) as db, db:
            db.execute("CREATE TABLE changes (note TEXT NOT NULL)")
            db.execute("INSERT INTO changes VALUES ('Original fixture row')")
        self.selector = root / "selected"
        self.selector.write_text(f"{PRIOR_PORT}\n", encoding="ascii")
        admission = root / "portal-admission"
        admission.write_text("synthetic-admission\n", encoding="ascii")
        self.units = {PRIOR_PORT: "fixture-a.service", CANDIDATE_PORT: "fixture-b.service"}
        self.settings = {
            "selector": str(self.selector), "environment_file": str(root / "serving.env"),
            "admission_file": str(admission), "python": sys.executable, "mount_path": "/tasks",
            "units": {str(port): unit for port, unit in self.units.items()},
        }
        self.processes = {self.units[PRIOR_PORT]: self.previous}
        self.events = []
        self.unhealthy_slot = None
        self.slots = ReleaseSlots(self.settings, self.layout.database, run=self.systemd)
        http = patch.object(self.slots, "_check_http", side_effect=self.direct_check)
        self.addCleanup(http.stop)
        http.start()
        pair = patch("release_slots.verify_rollback_ledger", autospec=True)
        self.addCleanup(pair.stop)
        self.pair_check = pair.start()

    @staticmethod
    def make_artifact(directory, contents):
        directory.mkdir()
        app = directory / "tasks"
        app.write_text(contents)
        manifest = directory / "SHA256SUMS"
        manifest.write_text(f"{file_hash(app)}  tasks\n", encoding="ascii")
        return file_hash(manifest)

    def systemd(self, command, **_options):
        if command[0] == "systemd-run":
            unit = next(argument.split("=", 1)[1] for argument in command if str(argument).startswith("--unit="))
            self.assertEqual(unit, self.units[CANDIDATE_PORT])
            self.assertIn(f"--property=WorkingDirectory={self.candidate}", command)
            self.assertIn(f"--property=EnvironmentFile={self.settings['environment_file']}", command)
            launcher = command.index(self.candidate / "tasks")
            self.assertEqual(command[launcher + 1:], [
                "--db", self.layout.database, "serve", "--portal-reader", "--websocket-revisions",
                "--host", "127.0.0.1", "--port", str(CANDIDATE_PORT),
            ])
            self.assertEqual(json.loads(self.journal.read_text())["slots"], [PRIOR_PORT, CANDIDATE_PORT])
            self.events.append(("start", CANDIDATE_PORT, self.slots.selected(), self.layout.current.resolve()))
            self.processes[unit] = self.candidate
            return CompletedProcess(command, 0, stdout="")
        self.assertEqual(command[:3], ["systemctl", "--user", "show"])
        directory = self.processes.get(command[3])
        if "--property=LoadState" in command:
            output = "loaded\n" if directory else "not-found\n"
        elif "--property=ActiveState" in command and "--value" in command:
            output = "active\n" if directory else "inactive\n"
        else:
            self.assertIsNotNone(directory)
            output = f"WorkingDirectory={directory}\nMainPID=123\nActiveState=active\n"
        return CompletedProcess(command, 0, stdout=output)

    def direct_check(self, port, credential):
        self.assertEqual(credential, "synthetic-admission")
        self.events.append(("direct", port, self.slots.selected(), self.layout.current.resolve()))
        if port == self.unhealthy_slot:
            raise MountedCheckFailed([{"name": "slot", "reason": "http_403"}])

    def smoke(self, digest):
        self.events.append(("mounted", digest, self.slots.selected(), self.layout.current.resolve()))
        return True

    def release(self, smoke=None):
        def restart():
            self.fail("The slot controller must leave the serving process running.")

        return run_release(
            self.layout, self.artifact, self.entry, restart, smoke or self.smoke, self.journal, slots=self.slots,
        )

    def test_candidate_is_pinned_and_healthy_before_routing_changes(self):
        result = self.release()
        self.assertEqual((result["status"], result["active"]), ("released", self.digest))
        self.assertEqual(self.events, [
            ("direct", PRIOR_PORT, PRIOR_PORT, self.previous),
            ("start", CANDIDATE_PORT, PRIOR_PORT, self.previous),
            ("direct", CANDIDATE_PORT, PRIOR_PORT, self.previous),
            ("mounted", self.digest, CANDIDATE_PORT, self.candidate),
        ])
        self.pair_check.assert_called_once_with(Path(sys.executable), self.previous, self.candidate)
        self.assertEqual(self.selector.read_text(encoding="ascii"), f"{CANDIDATE_PORT}\n")

    def test_failed_candidate_health_keeps_prior_slot_and_current_link(self):
        self.unhealthy_slot = CANDIDATE_PORT
        result = self.release()
        self.assertEqual(
            (result["status"], result["reason_code"], result["active"]),
            ("rejected", "candidate_slot_unavailable", self.base),
        )
        self.assertEqual(result["failed_checks"], [{"name": "slot", "reason": "http_403"}])
        self.assertEqual((self.slots.selected(), self.layout.current.resolve()), (PRIOR_PORT, self.previous))
        self.assertEqual(self.events[-1], ("direct", CANDIDATE_PORT, PRIOR_PORT, self.previous))

    def test_incompatible_ledger_pair_keeps_candidate_stopped(self):
        self.pair_check.side_effect = ValueError("The prior release cannot read the upgraded fixture ledger.")
        result = self.release()
        self.assertEqual((result["status"], result["active"]), ("rejected", self.base))
        self.assertEqual(self.events, [("direct", PRIOR_PORT, PRIOR_PORT, self.previous)])
        self.assertEqual(set(self.processes), {self.units[PRIOR_PORT]})
        self.assertEqual((self.slots.selected(), self.layout.current.resolve()), (PRIOR_PORT, self.previous))

    def test_failed_mounted_smoke_rolls_back_both_pointers_and_keeps_new_write(self):
        def smoke(digest):
            self.smoke(digest)
            if digest == self.digest:
                with closing(sqlite3.connect(self.layout.database)) as db, db:
                    db.execute("INSERT INTO changes VALUES ('Committed while the candidate served')")
                return False
            return True

        result = self.release(smoke)
        self.assertEqual((result["status"], result["active"]), ("rolled_back", self.base))
        self.assertEqual(self.events[-2:], [
            ("direct", PRIOR_PORT, CANDIDATE_PORT, self.candidate),
            ("mounted", self.base, PRIOR_PORT, self.previous),
        ])
        with closing(sqlite3.connect(self.layout.database)) as db:
            self.assertEqual(db.execute("SELECT note FROM changes ORDER BY rowid").fetchall(), [
                ("Original fixture row",), ("Committed while the candidate served",),
            ])
        with closing(sqlite3.connect(self.layout.backups / result["backup"])) as db:
            self.assertEqual(db.execute("SELECT note FROM changes").fetchall(), [("Original fixture row",)])

    def test_torn_pointer_updates_return_to_prior_without_relaunching_candidate(self):
        for selector_changed_first in (True, False):
            with self.subTest(selector_changed_first=selector_changed_first):
                write_json(self.journal, {
                    "candidate": self.digest, "base": self.base, "previous": self.previous.name,
                    "backup": "fixture.sqlite3", "execution": self.entry["execution"],
                    "slots": [PRIOR_PORT, CANDIDATE_PORT],
                })
                if not self.candidate.exists():
                    self.make_artifact(self.candidate, "candidate")
                if selector_changed_first:
                    self.slots.select(CANDIDATE_PORT)
                else:
                    switch_current(self.layout.current, self.candidate)
                self.events.clear()
                result = self.release()
                self.assertEqual(
                    (result["status"], result["reason_code"], result["active"]),
                    ("rolled_back", "activation_interrupted", self.base),
                )
                self.assertEqual([event[0] for event in self.events], ["direct", "mounted"])
                self.assertEqual((self.slots.selected(), self.layout.current.resolve()), (PRIOR_PORT, self.previous))
        self.pair_check.assert_not_called()

    def test_boot_restores_selected_release_even_after_interrupted_pointer_update(self):
        self.release()
        switch_current(self.layout.current, self.previous)
        self.processes.clear()
        self.events.clear()
        self.pair_check.reset_mock()

        self.slots.restore_selected(self.layout)

        self.assertEqual(self.processes, {self.units[CANDIDATE_PORT]: self.candidate})
        self.assertEqual(self.events, [
            ("start", CANDIDATE_PORT, CANDIDATE_PORT, self.previous),
            ("direct", CANDIDATE_PORT, CANDIDATE_PORT, self.previous),
        ])
        self.pair_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
