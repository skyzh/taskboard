"""Check the separately staged release tooling against a packaged app."""

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
from shutil import copytree
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ci_watch import export_commit, publish_artifact, resolve_commit
from local_queue import checked_results, fixture_verifier
from release_compatibility import certificate
from release_plan import ReleaseRejected, file_hash, verify_manifest


SOURCE = Path(__file__).resolve().parents[2]


class InstalledRunnerTest(unittest.TestCase):
    def test_packaged_app_works_with_the_launcher_and_pinned_checks(self):
        with TemporaryDirectory(prefix="taskboard-runner-ci-") as directory:
            root = Path(directory).resolve()
            tracked = root / "tracked"
            files = export_commit(SOURCE, resolve_commit(SOURCE, "HEAD"), tracked)
            # The CI job has already built the web app. Publish it with the tracked source
            # through the same artifact writer used by the Pi.
            copytree(SOURCE / "web/dist", tracked / "web/dist", dirs_exist_ok=True)
            artifacts = root / "artifacts"
            digest = publish_artifact(tracked, files, artifacts)
            app = artifacts / digest

            predecessor = root / "predecessor"
            pin_path = SOURCE / "tools/release/supported-runner.txt"
            commit = pin_path.read_text().strip()
            self.assertEqual(resolve_commit(SOURCE, commit), commit)
            export_commit(SOURCE, commit, predecessor)
            for name, checkout in (("supported predecessor", predecessor), ("candidate", tracked)):
                with self.subTest(runner=name):
                    self.check_runner(checkout / "tools/release", app, digest, root / (checkout.name + "-checks"))

    def check_runner(self, launcher, app, digest, scratch):
        scratch.mkdir()
        spec = spec_from_file_location("staged_compatibility_fixture", launcher / "compatibility_fixture.py")
        runner = module_from_spec(spec)
        spec.loader.exec_module(runner)
        try:
            with patch.dict(os.environ):
                os.environ.pop("TASKS_SERVER_CONFIG", None)
                with runner.fixture_server(sys.executable, app, scratch / "mounted", *certificate(scratch)) as server:
                    origin, cookie, database, env = server
                    actor = runner.seed(sys.executable, app, origin, cookie, database)
                    runner.verify_cli(sys.executable, app, env, origin, cookie, actor)
        except runner.CompatibilityFailure as error:
            diagnostics = scratch / "mounted/service.log"
            output = diagnostics.read_text() if diagnostics.is_file() else ""
            self.fail(f"Packaged app failed the installed-launcher fixture: {error}\n{output[-1500:]}")

        package = launcher / "checks"
        pin = file_hash(package / "SHA256SUMS")
        try:
            members = verify_manifest(package, pin)
            self.assertIn(Path("fixture_checks.py"), members)
            self.assertIn(Path("release_smoke.py"), members)
            verify = fixture_verifier(package, pin, sys.executable, ("priority", "restart"))
            job = {"candidate": digest}
            reports = verify(app, job)
            checked_results(job, reports)
        except ReleaseRejected as error:
            diagnostics = getattr(error.__cause__, "stdout", None) or ""
            self.fail(f"Staged check package failed. Verify its tracked manifest and review any "
                      f"required operator package update separately. {error}\n{diagnostics[-2000:]}")
        self.assertEqual(
            [report["id"] for report in reports[0]["checks"]],
            ["artifact-manifest", "priority", "restart"],
        )
