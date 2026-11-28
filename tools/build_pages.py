"""Package the reviewed Summary demo with the public Taskboard homepage."""

import argparse
from pathlib import Path
from shutil import copyfile

from check_summary_demo import CARD_ASSET, check, read_public_file

HOMEPAGE_FILES = ("homepage.css", "homepage-motion.js", "legacy-demo.js", "sitemap.xml")
SQUAD_AVATARS = ("beacon", "loom", "prism", "gauge", "herald", "caliper", "archivist", "meter")


def page_with_tracking(source, tracking, *, head_metadata=b""):
    page = read_public_file(source, source.name)
    if page.count(b"</head>") != 1:
        raise ValueError(f"Expected one closing head tag in {source}")
    return page.replace(b"</head>", head_metadata + tracking + b"</head>", 1)


def build(demo, homepage, output):
    artifacts = check(demo)
    home_files = {name: read_public_file(homepage / name, name) for name in HOMEPAGE_FILES}
    portrait_source = Path(__file__).resolve().parents[1] / "docs/sponsor/avatars"
    portraits = {name: read_public_file(portrait_source / f"{name}.svg", f"portrait for {name}") for name in SQUAD_AVATARS}
    tracking = read_public_file(homepage / "analytics.html", "analytics.html")
    home_index = page_with_tracking(homepage / "index.html", tracking)
    demo_index = page_with_tracking(
        demo / "index.html", tracking, head_metadata=b'  <meta name="robots" content="noindex,follow">\n',
    )
    output.mkdir(parents=True)
    for relative in artifacts.keys() - {"index.html"}:
        destination = output / "demo" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(demo / relative, destination)
    (output / CARD_ASSET).parent.mkdir(parents=True, exist_ok=True)
    copyfile(demo / CARD_ASSET, output / CARD_ASSET)
    for name, data in portraits.items():
        (output / "assets" / f"squad-{name}.svg").write_bytes(data)
    (output / "index.html").write_bytes(home_index)
    (output / "demo" / "index.html").write_bytes(demo_index)
    for name, data in home_files.items():
        (output / name).write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("demo", type=Path)
    parser.add_argument("homepage", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.demo, args.homepage, args.output)
