"""Regression for AppleDouble entries in a staged release transfer."""

from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest

from release_plan import ReleaseRejected, verify_manifest
from stage_transfer import stage_transfer, verify_archive


class StageTransferTest(unittest.TestCase):
    def test_archive_excludes_appledouble_and_keeps_the_manifest_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            source = artifact / "nested" / "sample.txt"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"Review this sample.\n")
            source.chmod(0o755)
            manifest = artifact / "SHA256SUMS"
            manifest.write_text(f"{sha256(source.read_bytes()).hexdigest()}  nested/sample.txt\n")
            digest = sha256(manifest.read_bytes()).hexdigest()

            if sys.platform == "darwin":
                subprocess.run(["xattr", "-w", "com.example.taskboard", "fixture", source], check=True)
                env = dict(os.environ)
                env.pop("COPYFILE_DISABLE", None)
                plain = root / "plain.tar"
                subprocess.run(
                    ["/usr/bin/tar", "-cf", plain, "-C", artifact, "SHA256SUMS", "nested/sample.txt"],
                    env=env, check=True,
                )
                with tarfile.open(plain, "r:") as package:
                    self.assertIn("nested/._sample.txt", package.getnames())
                with self.assertRaisesRegex(ReleaseRejected, "exactly the manifest"):
                    verify_archive(plain, {"SHA256SUMS", "nested/sample.txt"})

            (source.parent / "._sample.txt").write_bytes(b"Unlisted file.")
            output = root / "app.tar"
            self.assertEqual(stage_transfer(artifact, digest, output), 2)
            staged = root / "staged"
            with tarfile.open(output, "r:") as package:
                self.assertEqual(package.getnames(), ["SHA256SUMS", "nested/sample.txt"])
                for member in package:
                    self.assertTrue(member.isfile())
                    payload = package.extractfile(member)
                    destination = staged / member.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with payload:
                        destination.write_bytes(payload.read())
                self.assertEqual(package.getmember("nested/sample.txt").mode & 0o111, 0o111)
            self.assertEqual(verify_manifest(staged, digest), [Path("nested/sample.txt")])

    def test_bad_artifact_does_not_publish_an_archive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SHA256SUMS").write_text("different manifest\n")
            output = root / "app.tar"
            with self.assertRaisesRegex(ReleaseRejected, "reviewed digest"):
                stage_transfer(root, "a" * 64, output)
            self.assertFalse(output.exists())
