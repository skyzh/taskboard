"""Check that the Pages payload contains only the reviewed Summary assets."""

from hashlib import sha256
import json
from pathlib import Path
import re
import sys


def check(root):
    allowed = re.compile(r"index\.html|assets/[a-zA-Z0-9_.-]+\.(css|js|png|svg)|assets/licenses/[a-zA-Z0-9_.-]+")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Do not publish a symlink: {path.relative_to(root)}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or not allowed.fullmatch(relative):
            raise ValueError(f"Unexpected Pages asset: {relative}")
        data = path.read_bytes()
        if any(value in data.lower() for value in (b".slock/", b"chi-net.", b"skyzh-raspi", b"-----begin private key-----")):
            raise ValueError(f"Private deployment material found in {relative}")
        files[relative] = sha256(data).hexdigest()
    if "index.html" not in files or not any(path.endswith(".js") for path in files):
        raise ValueError("The Summary entry page or its script is missing.")
    return files


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=2, sort_keys=True))
