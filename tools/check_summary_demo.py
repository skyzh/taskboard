"""Check that the Pages payload contains only the reviewed Summary assets."""

from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from struct import pack
import sys

CARD_ASSET = "assets/taskboard-social-card.png"
CARD_URL = f"https://skyzh.github.io/taskboard/{CARD_ASSET}"


class PreviewMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attributes):
        if tag == "meta":
            fields = dict(attributes)
            name = fields.get("name") or fields.get("property")
            self.values[name] = fields.get("content")


def check_social_card(root, files):
    metadata = PreviewMetadata()
    metadata.feed((root / "index.html").read_text(encoding="utf-8"))
    expected = {
        "twitter:card": "summary_large_image",
        "twitter:image": CARD_URL,
        "og:image": CARD_URL,
        "og:image:type": "image/png",
        "og:image:width": "1200",
        "og:image:height": "600",
    }
    for name, value in expected.items():
        if metadata.values.get(name) != value:
            raise ValueError(f"Incorrect social card metadata: {name}")
    for name in ("twitter:image:alt", "og:image:alt"):
        if not (metadata.values.get(name) or "").strip():
            raise ValueError(f"Missing social card alt text: {name}")
    png_header = b"\x89PNG\r\n\x1a\n" + pack(">I4sII", 13, b"IHDR", 1200, 600)
    if CARD_ASSET not in files or not (root / CARD_ASSET).read_bytes().startswith(png_header):
        raise ValueError("The Pages artifact needs the 1200 × 600 PNG social card.")


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
    check_social_card(root, files)
    return files


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), indent=2, sort_keys=True))
