"""Exercise the local archive prerequisite without installing a browser."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from verify_pi_browser_archives import ARCHITECTURE, PACKAGES, VERSION, main, verify_archives


class PiBrowserArchiveTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archives = {}
        for package in PACKAGES:
            archive = self.root / f"{package}.deb"
            archive.write_bytes(f"Fictional {package} archive".encode())
            self.archives[package] = (archive, sha256(archive.read_bytes()).hexdigest())

    def metadata(self, command, **_options):
        self.assertEqual(command[0], "dpkg-deb")
        self.assertEqual(command[-2], "--show")
        return f"{command[-1].stem}\t{VERSION}\t{ARCHITECTURE}\n"

    def test_all_approved_digests_are_verified_before_reading_metadata(self):
        with patch("verify_pi_browser_archives.subprocess.check_output", side_effect=self.metadata) as metadata:
            proof = verify_archives(self.archives)
        self.assertEqual(proof, {"architecture": ARCHITECTURE, "version": VERSION,
                                 "sha256": {package: value[1] for package, value in self.archives.items()}})
        self.assertEqual(metadata.call_count, 3)

    def test_missing_malformed_and_incorrect_pins_prevent_package_tools(self):
        chromium, digest = self.archives["chromium"]
        for archives in (
            {key: value for key, value in self.archives.items() if key != "chromium-sandbox"},
            {**self.archives, "chromium": (chromium, "")},
            {**self.archives, "chromium": (chromium, digest.upper())},
            {**self.archives, "chromium": (chromium, "f" * 64)},
        ):
            with (self.subTest(archives=archives),
                  patch("verify_pi_browser_archives.subprocess.check_output") as metadata,
                  self.assertRaisesRegex(ValueError, "approved")):
                verify_archives(archives)
            metadata.assert_not_called()

    def test_valid_archive_pin_must_still_match_expected_package_identity(self):
        for metadata in (
            f"chromium-browser\t{VERSION}\t{ARCHITECTURE}\n",
            f"chromium\t{VERSION}\tamd64\n",
            f"chromium\t{VERSION.replace('151.', '150.')}\t{ARCHITECTURE}\n",
        ):
            with (self.subTest(metadata=metadata),
                  patch("verify_pi_browser_archives.subprocess.check_output", return_value=metadata),
                  self.assertRaisesRegex(ValueError, "exact replay version and architecture")):
                verify_archives(self.archives)

    def test_cli_only_emits_receipt_after_all_pins_and_metadata_match(self):
        args = ["verify_pi_browser_archives.py"]
        for package, (archive, digest) in self.archives.items():
            args.extend((f"--{package}", str(archive), digest))
        with (patch("sys.argv", args), redirect_stdout(StringIO()) as output,
              patch("verify_pi_browser_archives.subprocess.check_output", side_effect=self.metadata)):
            main()
        self.assertEqual(json.loads(output.getvalue())["version"], VERSION)

        with (patch("sys.argv", args[:-1]), redirect_stdout(StringIO()) as output,
              redirect_stderr(StringIO()), self.assertRaises(SystemExit),
              patch("verify_pi_browser_archives.subprocess.check_output") as metadata):
            main()
        self.assertEqual(output.getvalue(), "")
        metadata.assert_not_called()


if __name__ == "__main__":
    unittest.main()
