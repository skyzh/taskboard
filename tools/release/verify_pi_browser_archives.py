"""Verify locally staged Chromium archives for a disposable Pi runtime replay."""

import argparse
from hashlib import file_digest
import json
from pathlib import Path
import subprocess

from queue_provenance import SHA256


VERSION = "1:151.0.7922.108-1~deb13u1+rpt1"
ARCHITECTURE = "arm64"
PACKAGES = ("chromium", "chromium-common", "chromium-sandbox")


def verify_archives(archives):
    if set(archives) != set(PACKAGES) or not all(SHA256.fullmatch(digest) for _, digest in archives.values()):
        raise ValueError("Provide all three Chromium archives and their approved lowercase SHA-256 digests.")

    verified = {}
    for package in PACKAGES:
        source, expected = archives[package]
        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"The staged {package} archive must be a regular file.")
        with path.open("rb") as archive:
            observed = file_digest(archive, "sha256").hexdigest()
        if observed != expected:
            raise ValueError(f"The staged {package} archive does not match the approved SHA-256.")
        verified[package] = path

    for package, path in verified.items():
        fields = subprocess.check_output([
            "dpkg-deb", r"--showformat=${Package}\t${Version}\t${Architecture}\n", "--show", path,
        ], text=True, timeout=20).rstrip("\n").split("\t")
        if fields != [package, VERSION, ARCHITECTURE]:
            raise ValueError(f"The staged {package} archive must be the exact replay version and architecture.")

    return {"version": VERSION, "architecture": ARCHITECTURE,
            "sha256": {package: archives[package][1] for package in PACKAGES}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for package in PACKAGES:
        parser.add_argument(f"--{package}", required=True, nargs=2, metavar=("DEB", "APPROVED_SHA256"))
    args = parser.parse_args()
    try:
        proof = verify_archives({package: getattr(args, package.replace("-", "_")) for package in PACKAGES})
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"The Chromium archives were not approved for replay: {error}\n")
    print(json.dumps(proof, separators=(",", ":")))


if __name__ == "__main__":
    main()
