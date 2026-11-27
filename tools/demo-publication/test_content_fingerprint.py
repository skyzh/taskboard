from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from content_fingerprint import MANIFEST, compare_package, fingerprint, latest_deployment, public_fingerprint, verify_legacy_package

REVISION = "a" * 40
URL = "https://pages.example.test/board"


class ContentFingerprintTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.package = Path(temporary.name)
        (self.package / "demo").mkdir()
        (self.package / "index.html").write_text("homepage")
        (self.package / "demo/index.html").write_text("demo")

    def test_same_content_skips_and_changed_homepage_or_removed_file_publishes(self):
        previous = fingerprint(self.package)
        self.assertFalse(compare_package(self.package, REVISION, previous))
        self.assertEqual(json.loads((self.package / MANIFEST).read_text())["sha256"], previous)
        (self.package / "index.html").write_text("new homepage")
        self.assertTrue(compare_package(self.package, REVISION, previous))
        self.assertFalse(compare_package(self.package, REVISION, previous, self.package))
        asset = self.package / "demo/asset.js"
        asset.write_text("demo asset")
        added = fingerprint(self.package)
        asset.unlink()
        self.assertTrue(compare_package(self.package, REVISION, added))

    def test_only_the_verified_successful_release_is_selected(self):
        def read(path):
            if "/deployments?" in path:
                return [{"id": 2, "environment": "github-pages", "sha": "b" * 40},
                        {"id": 1, "environment": "github-pages", "sha": REVISION}]
            return [{"state": "failure"}] if "/2/" in path else [{"state": "success", "environment_url": URL}]
        self.assertEqual(latest_deployment("example/board", read), (REVISION, URL))

    def test_skipped_environment_job_cannot_hide_the_last_pages_release(self):
        def read(path):
            if "/deployments?" in path:
                return [{"id": 2, "environment": "github-pages", "sha": "b" * 40},
                        {"id": 1, "environment": "github-pages", "sha": REVISION}]
            if "/2/" in path:
                return [{"state": "success", "environment_url": ""}]
            return [{"state": "success", "environment_url": URL}]

        self.assertEqual(latest_deployment("example/board", read), (REVISION, URL))

    def test_manifest_requires_the_deployed_revision(self):
        def open_page(url, *, timeout):
            self.assertEqual(url, f"{URL}/{MANIFEST}?revision={REVISION}")
            return BytesIO(json.dumps({"schema": 1, "revision": "b" * 40, "sha256": "c" * 64}).encode())
        with self.assertRaisesRegex(ValueError, "does not match"):
            public_fingerprint(URL, REVISION, open_page)

    def test_missing_manifest_uses_only_a_verified_legacy_package(self):
        def missing(url, *, timeout):
            raise HTTPError(url, 404, "legacy fixture", {}, BytesIO())
        self.assertIsNone(public_fingerprint(URL, REVISION, missing))

        def legacy(url, *, timeout):
            return BytesIO(b"homepage" if url == f"{URL}/index.html" else b"demo")
        verify_legacy_package(self.package, URL, legacy)
        with self.assertRaisesRegex(ValueError, "does not match public file"):
            verify_legacy_package(self.package, URL, lambda *_, **__: BytesIO(b"wrong bytes"))


if __name__ == "__main__":
    unittest.main()
