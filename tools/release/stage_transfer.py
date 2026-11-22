"""Prepare an exact, manifest-only app archive before transfer to release staging."""

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from tempfile import TemporaryDirectory

from release_plan import ReleaseRejected, verify_manifest


def verify_archive(archive, expected_names):
    with tarfile.open(archive, "r:") as package:
        members = package.getmembers()
    if (
        len(members) != len(expected_names)
        or {member.name for member in members} != expected_names
        or any(not member.isfile() for member in members)
    ):
        raise ReleaseRejected("The transfer archive must contain exactly the manifest's regular files and SHA256SUMS.")


def stage_transfer(artifact, manifest_sha256, output):
    names = ["SHA256SUMS", *(path.as_posix() for path in verify_manifest(artifact, manifest_sha256))]
    with TemporaryDirectory(prefix=".taskboard-transfer-", dir=output.parent) as directory:
        archive = Path(directory) / "release.tar"
        # Python's tar writer does not emit macOS AppleDouble metadata members.
        with tarfile.open(archive, "w") as package:
            for name in names:
                package.add(artifact / name, arcname=name, recursive=False)
        verify_archive(archive, set(names))
        os.link(archive, output)
    return len(names)


def extract_transfer(source, destination, manifest_sha256):
    """Read regular archive members into an empty, private operator directory."""
    names = set()
    with tarfile.open(fileobj=source, mode="r|") as package:
        for member in package:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or not name.parts or name.as_posix() != member.name or name in names):
                raise ReleaseRejected("Unexpected file in the app transfer.", "artifact")
            names.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.extractfile(member) as contents, target.open("xb") as output:
                shutil.copyfileobj(contents, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    expected = {"SHA256SUMS", *(path.as_posix() for path in verify_manifest(destination, manifest_sha256))}
    if {name.as_posix() for name in names} != expected:
        raise ReleaseRejected("The transfer contains files outside the exact manifest.", "artifact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        files = stage_transfer(args.artifact, args.manifest_sha256, args.output)
    except (OSError, ReleaseRejected, tarfile.TarError) as error:
        parser.exit(1, f"stage transfer: {error}\n")
    print(f"Verified {files} regular files in {args.output}")


if __name__ == "__main__":
    main()
