"""Build the four Taskboard wheels from checkout source and the built React app."""

import argparse
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from checkout import ROOT


COMPONENTS = ("client", "cli", "mcp", "server")


def build(output):
    dist = ROOT / "web" / "dist"
    if not (dist / "index.html").is_file():
        raise ValueError("Build the web app with npm --prefix web run build first.")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="taskboard-wheel-") as directory:
        for component in COMPONENTS:
            source = ROOT / "packages" / component
            stage = Path(directory) / component
            shutil.copytree(source / "src", stage / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            shutil.copy2(source / "pyproject.toml", stage)
            shutil.copy2(ROOT / "LICENSE", stage)
            if component == "server":
                shutil.copytree(dist, stage / "src/taskboard_server/web")
            subprocess.run(["uv", "build", "--wheel", "--no-build-logs", "--out-dir", output, stage], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    build(args.out_dir.resolve())


if __name__ == "__main__":
    main()
