"""Compare public Pages content against the most recent successful deployment."""

from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

from github_read import read as read_github

MANIFEST = "summary-content.json"
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")


def fingerprint(root):
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != root / MANIFEST)
    if not paths or not (root / "index.html").is_file() or not (root / "demo/index.html").is_file():
        raise ValueError("The packaged homepage or demo is missing.")
    rows = [(path.relative_to(root).as_posix(), sha256(path.read_bytes()).hexdigest()) for path in paths]
    return sha256(json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def latest_deployment(repository, read):
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("Invalid workflow repository.")
    repo = f"/repos/{repository}"
    filters = urlencode({"environment": "github-pages", "per_page": 20})
    for deployment in read(f"{repo}/deployments?{filters}"):
        if deployment["environment"] != "github-pages":
            continue
        states = read(f"{repo}/deployments/{int(deployment['id'])}/statuses?per_page=1")
        if not states or states[0]["state"] != "success":
            continue
        revision = deployment["sha"]
        raw_url = states[0].get("environment_url") or ""
        # A successful environment job can skip deploy-pages after the final main check.
        if not raw_url:
            continue
        page = urlsplit(raw_url)
        if (not isinstance(revision, str) or not COMMIT.fullmatch(revision)
                or any(not char.isprintable() or char.isspace() for char in raw_url)
                or page.scheme != "https" or not page.hostname
                or page.username is not None or page.query or page.fragment):
            raise ValueError("The last verified Pages deployment does not identify its commit and HTTPS site.")
        return revision, raw_url.rstrip("/")
    raise ValueError("No successful Pages deployment was found. Use the checked manual publisher to initialize Pages.")


def public_fingerprint(url, revision, open_page=urlopen):
    try:
        with open_page(f"{url}/{MANIFEST}?revision={revision}", timeout=15) as response:
            content = response.read(4097)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        raise
    if len(content) > 4096:
        raise ValueError("The published content fingerprint is oversized.")
    manifest = json.loads(content)
    if (not isinstance(manifest, dict) or manifest.get("revision") != revision or manifest.get("schema") != 1
            or not isinstance(manifest.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])):
        raise ValueError("The published content fingerprint does not match the verified Pages deployment.")
    return manifest["sha256"]


def verify_legacy_package(package, url, open_page=urlopen):
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        expected = path.read_bytes()
        with open_page(f"{url}/{quote(relative)}", timeout=15) as response:
            if response.read(len(expected) + 1) != expected:
                raise ValueError(f"The rebuilt prior deployment does not match public file {relative}.")


def compare_package(package, revision, previous, legacy_package=None):
    if not COMMIT.fullmatch(revision):
        raise ValueError("Choose the full protected-main revision.")
    if legacy_package is not None:
        previous = fingerprint(legacy_package)
    candidate = fingerprint(package)
    (package / MANIFEST).write_text(json.dumps({"schema": 1, "revision": revision, "sha256": candidate}) + "\n",
                                    encoding="utf-8")
    return previous != candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("previous", help="Resolve the live deployment and its content fingerprint.")
    compare = modes.add_parser("compare", help="Write the receipt and compare the packaged files.")
    compare.add_argument("package", type=Path)
    compare.add_argument("revision")
    compare.add_argument("--previous", default="")
    compare.add_argument("--legacy-package", type=Path)
    compare.add_argument("--legacy-url")
    args = parser.parse_args()
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        if args.mode == "previous":
            revision, url = latest_deployment(os.environ["GITHUB_REPOSITORY"], read_github)
            previous = public_fingerprint(url, revision)
            print(f"revision={revision}", file=output)
            print(f"sha256={previous or ''}", file=output)
            print(f"url={url}", file=output)
        else:
            if args.legacy_package is not None:
                if not args.legacy_url:
                    parser.error("Supply the verified deployment URL for the legacy package.")
                verify_legacy_package(args.legacy_package, args.legacy_url)
            changed = compare_package(args.package, args.revision, args.previous, args.legacy_package)
            print(f"changed={str(changed).lower()}", file=output)


if __name__ == "__main__":
    main()
