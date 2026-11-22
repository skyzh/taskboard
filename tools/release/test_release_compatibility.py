"""Public synthetic release compatibility checks."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from admit_review import enqueue_admitted
from compatibility_fixture import (
    MANIFEST, CompatibilityFailure, actions, fixture_server, read_json, seed, verify_cli,
    verify_contract, verify_queue,
)
from release_compatibility import SCHEMA, certificate, supported_clients, verify_missing_action, verify_receipt
from release_plan import ReleaseRejected


SOURCE = Path(__file__).resolve().parents[2]
BASE, HEAD, LEGACY = "a" * 64, "b" * 64, "c" * 64


def fixture_receipt(base=BASE):
    cases = [("candidate", HEAD, base), ("legacy", LEGACY, HEAD), ("predecessor", base, HEAD)]
    return {
        "schema": SCHEMA, "status": "pass", "candidate": HEAD, "base": base,
        "pairings": [{"client": client, "server": server, "roles": [role], "workflow": "start-wait-resume"}
                     for role, client, server in cases],
        "browser": {"client": base, "server": HEAD, "rendered": True, "assets": {"fixture.js": "d" * 64}},
    }


class ReleaseCompatibilityTest(unittest.TestCase):
    def test_mounted_shared_cli_keeps_its_default_routes_and_receipts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with fixture_server(sys.executable, SOURCE, root / "board", *certificate(root)) as server:
                origin, cookie, database, env = server
                actor = seed(sys.executable, SOURCE, origin, cookie, database)
                manifest = actions(read_json(origin, MANIFEST))
                verify_queue(origin, cookie)
                verify_cli(sys.executable, SOURCE, env, origin, cookie, actor)
        added = deepcopy(manifest)
        added["context"]["parameters"]["detail"] = {"type": "string", "required": False}
        verify_contract(manifest, added)
        added["context"]["parameters"]["detail"]["required"] = True
        with self.assertRaisesRegex(CompatibilityFailure, "added required parameter"):
            verify_contract(manifest, added)

    def test_first_release_client_is_required_after_bootstrap_and_pins_are_verified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tasks").write_text("synthetic fixture\n")
            manifest = root / "SHA256SUMS"
            manifest.write_text(f"{sha256((root / 'tasks').read_bytes()).hexdigest()}  tasks\n")
            digest = sha256(manifest.read_bytes()).hexdigest()
            input_pin = {"artifact": str(root), "manifest": digest}
            policy = {"legacy_client": input_pin}
            with self.assertRaisesRegex(CompatibilityFailure, "retained first real client"):
                supported_clients(policy, root, digest)
            clients = supported_clients({**policy, "first_real_client": input_pin}, root, digest)
            self.assertEqual(clients[digest]["roles"], ["legacy", "predecessor", "first-real"])
            (root / "tasks").write_text("changed fixture")
            with self.assertRaises(ReleaseRejected):
                supported_clients({**policy, "first_real_client": input_pin}, root, digest)

    def test_manual_receipt_needs_exact_pins_and_every_required_pairing(self):
        receipt = fixture_receipt()
        verify_receipt(receipt, HEAD, BASE, legacy=LEGACY)
        with self.assertRaisesRegex(CompatibilityFailure, "exact candidate and base"):
            verify_receipt(receipt, HEAD, "e" * 64, legacy=LEGACY)
        with self.assertRaisesRegex(CompatibilityFailure, "required client pairing"):
            verify_receipt(receipt, HEAD, BASE, legacy=LEGACY, first_real=BASE)
        with self.assertRaisesRegex(CompatibilityFailure, "required unavailable action"):
            verify_receipt(receipt, HEAD, BASE, legacy=LEGACY, unavailable_action="fixture-new")
        receipt["pairings"][0]["unavailable_action"] = "fixture-new"
        verify_receipt(receipt, HEAD, BASE, legacy=LEGACY, unavailable_action="fixture-new")
        receipt["browser"] = {"client": BASE, "server": HEAD, "omitted": "Fixture has no web bundle."}
        with self.assertRaisesRegex(CompatibilityFailure, "predecessor browser"):
            verify_receipt(receipt, HEAD, BASE, legacy=LEGACY)
        verify_receipt(receipt, HEAD, BASE, legacy=LEGACY, browser_required=False)

    def test_unavailable_action_must_fail_before_changing_ledger(self):
        policy = {"unavailable_action": {"action": "fixture-new", "command": ["fixture-command"]}}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mounted = ("https://127.0.0.1:1", "synthetic-cookie", root / "ledger.sqlite3", {})
            def command(_python, _candidate, settings, *argv):
                self.assertEqual(argv, ("fixture-command",))
                Path(settings["TASKBOARD_COMPAT_MISSING_ACTION"]).write_text("fixture-new")
                return subprocess.CompletedProcess(argv, 1, "", "Raft rejected the invocation before dispatch.")

            with (patch("release_compatibility.cli", side_effect=command),
                  patch("release_compatibility.read_json", return_value={"revision": "fixture-revision"})):
                self.assertEqual(verify_missing_action(policy, root, {"fixture-new": {}}, {}, mounted,
                                                       sys.executable), "fixture-new")
            with (patch("release_compatibility.cli", side_effect=command),
                  patch("release_compatibility.read_json", side_effect=[{"revision": "before"}, {"revision": "after"}])):
                with self.assertRaisesRegex(CompatibilityFailure, "ledger revision"):
                    verify_missing_action(policy, root, {"fixture-new": {}}, {}, mounted, sys.executable)

    def test_failed_compatibility_never_creates_the_admission_entry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue"
            commit = "d" * 40
            state = root / "state"
            (state / "commits").mkdir(parents=True)
            (state / "commits" / f"{commit}.json").write_text(json.dumps({"commit": commit}))
            active = root / "SHA256SUMS"
            active.write_text("fixture manifest")
            policy = {"legacy_client": {"manifest": LEGACY}}
            with (patch("admit_review.read_reviewed_job", return_value={"candidate": HEAD}),
                  patch("admit_review.verify_manifest"),
                  patch("admit_review.run_matrix", side_effect=CompatibilityFailure("failed case"))):
                with self.assertRaisesRegex(CompatibilityFailure, "failed case"):
                    enqueue_admitted(None, state, queue, active, commit, 1, 2, (), "T-78",
                                     compatibility_policy=policy)
            self.assertFalse(queue.exists())


    def test_admission_reuses_the_saved_receipt_when_browser_metadata_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue, state, commit = root / "queue", root / "state", "d" * 40
            (state / "commits").mkdir(parents=True)
            (state / "commits" / f"{commit}.json").write_text(json.dumps({"commit": commit}))
            active = root / "SHA256SUMS"
            active.write_text("fixture manifest")
            first = fixture_receipt(sha256(active.read_bytes()).hexdigest())
            second = deepcopy(first)
            first["browser"]["chromium"] = "first-version"
            second["browser"]["chromium"] = "next-version"
            job = {"candidate": HEAD, "release_id": "fixture-release"}
            policy = {"legacy_client": {"manifest": LEGACY}}
            with (patch("admit_review.read_reviewed_job", side_effect=[dict(job), dict(job)]),
                  patch("admit_review.verify_manifest"),
                  patch("admit_review.run_matrix", side_effect=[first, second])):
                initial = enqueue_admitted(None, state, queue, active, commit, 1, 2, (), "T-78",
                                           compatibility_policy=policy)
                replay = enqueue_admitted(None, state, queue, active, commit, 1, 2, (), "T-78",
                                          compatibility_policy=policy)
            self.assertEqual(replay, initial)
            self.assertEqual(replay["compatibility"]["browser"]["chromium"], "first-version")
            self.assertEqual(json.loads((queue / "fixture-release.json").read_text()), initial)
