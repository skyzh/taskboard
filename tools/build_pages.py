"""Package the reviewed Summary demo with the public Taskboard homepage."""

import argparse
from pathlib import Path
from shutil import copyfile

from check_summary_demo import check, read_public_file

HOMEPAGE_FILES = ("index.html", "homepage.css", "legacy-demo.js")


def build(demo, homepage, output):
    artifacts = check(demo)
    home_files = {name: read_public_file(homepage / name, name) for name in HOMEPAGE_FILES}
    output.mkdir(parents=True)
    for relative in artifacts:
        destination = output / ("demo.html" if relative == "index.html" else relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(demo / relative, destination)
    for name, data in home_files.items():
        (output / name).write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("demo", type=Path)
    parser.add_argument("homepage", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.demo, args.homepage, args.output)
