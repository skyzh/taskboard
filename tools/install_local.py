#!/usr/bin/env python3
"""Install a reviewed Taskboard source tree for the current user."""

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from tempfile import mkdtemp


SOURCE = Path(__file__).resolve().parents[1]


def check_existing_path(path, *, directory):
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected_type(existing.st_mode) or existing.st_uid != os.geteuid()
            or existing.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        kind = "directory" if directory else "regular file"
        raise OSError(f"Unsafe install path {path}. Use an owned {kind} with group and other writes disabled.")


def copy_source(source, destination):
    for name in ("tasks", "pyproject.toml", "uv.lock"):
        shutil.copy2(source / name, destination / name)
    tools = destination / "tools"
    tools.mkdir()
    shutil.copy2(source / "tools/checkout.py", tools)
    for component in ("client", "cli", "server"):
        relative = Path("packages") / component / "src"
        shutil.copytree(source / relative, destination / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))

    web = destination / "web"
    web.mkdir()
    for name in ("index.html", "package.json", "package-lock.json", "vite.config.js"):
        shutil.copy2(source / "web" / name, web / name)
    for name in ("public", "src"):
        shutil.copytree(source / "web" / name, web / name)
    prompts = Path("docs/taskboard/prompts/v1")
    shutil.copytree(source / prompts, destination / prompts)


def install(root, *, offline):
    directories = (root / "releases", root / "bin", root / "data")
    for path in (root, *directories):
        check_existing_path(path, directory=True)
    for name in ("current", "bin/taskboard", "data/taskboard.sqlite3", "data/taskboard.sqlite3-wal",
                 "data/taskboard.sqlite3-shm", "data/taskboard.sqlite3-journal"):
        check_existing_path(root / name, directory=False)

    for path in directories:
        path.mkdir(parents=True, exist_ok=True)
    release = Path(mkdtemp(prefix="build-", dir=root / "releases"))
    try:
        copy_source(SOURCE, release)
        cache_only = ["--offline"] if offline else []
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-dev", "--python", sys.executable, "--no-python-downloads", *cache_only],
            cwd=release, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(release / ".venv")}, check=True,
        )
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund", *cache_only], cwd=release / "web", check=True)
        subprocess.run(["npm", "run", "build"], cwd=release / "web", check=True)

        launcher = release / "activate-launcher"
        shutil.copyfile(SOURCE / "tools" / "taskboard-local.sh", launcher)
        launcher.chmod(0o700)
        selection = release / "activate-release"
        selection.write_text(release.name + "\n", encoding="utf-8")
        launcher.replace(root / "bin" / "taskboard")
        selection.replace(root / "current")
    except (OSError, subprocess.CalledProcessError):
        shutil.rmtree(release)
        raise
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    parser.add_argument("--root", type=Path, default=data_home / "taskboard", help="Install directory.")
    parser.add_argument("--offline", action="store_true", help="Use cached dependencies only.")
    args = parser.parse_args()
    root = args.root.expanduser().absolute()
    os.umask(0o077)
    try:
        release = install(root, offline=args.offline)
    except (OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Taskboard installation failed: {error}\n")
    print(f"Installed Taskboard at {root / 'bin' / 'taskboard'}")
    print(f"Ledger: {root / 'data' / 'taskboard.sqlite3'}")
    print(f"Build: {release}")


if __name__ == "__main__":
    main()
