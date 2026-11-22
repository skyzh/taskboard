#!/usr/bin/env python3
"""Exercise a fresh local install, real preview restarts, and an in-place upgrade."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

from install_local import SOURCE, check_existing_path, copy_source


loopback = build_opener(ProxyHandler({}))


def get(url, *, as_json=False):
    with loopback.open(url, timeout=2) as response:
        return json.load(response) if as_json else response.read().decode()


@contextmanager
def preview(launcher, working, env, port):
    url = f"http://127.0.0.1:{port}/"
    with TemporaryFile() as output, subprocess.Popen(
        [launcher, "serve", "--port", str(port)], cwd=working, env=env, stdout=output, stderr=output,
    ) as process:
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if get(url + "healthz", as_json=True) == {"status": "ok"}:
                        break
                except (OSError, URLError):
                    pass
                time.sleep(0.05)
            else:
                output.seek(0)
                raise RuntimeError(f"Preview did not start.\n{output.read().decode()}")
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def check(*, offline):
    with TemporaryDirectory(prefix="taskboard install proof ") as directory:
        working = Path(directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("TASKS_")}
        env["XDG_DATA_HOME"] = str(working / "user data")
        root = Path(env["XDG_DATA_HOME"]) / "taskboard"
        launcher = root / "bin" / "taskboard"
        db = root / "data" / "taskboard.sqlite3"
        cache_only = ["--offline"] if offline else []

        def install(source, *, install_root=None, expect_failure=False):
            selected_root = ["--root", install_root] if install_root else []
            return subprocess.run(
                [sys.executable, "-B", source / "tools" / "install_local.py", *cache_only, *selected_root],
                cwd=working, env=env, check=not expect_failure, capture_output=expect_failure, text=True,
            )

        def tasks(*args):
            return subprocess.check_output([launcher, *args], cwd=working, env=env, text=True)

        def task(url):
            return get(url + "api/v1/tasks/1", as_json=True)["task"]

        external = working / "external fixture"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("Preserve this file.")
        root_link = working / "root symlink"
        root_link.symlink_to(external, target_is_directory=True)
        rejected_paths = [(root_link, root_link)]
        for name in ("data", "bin"):
            linked = working / f"{name} symlink"
            linked.mkdir()
            (linked / name).symlink_to(external, target_is_directory=True)
            rejected_paths.append((linked, linked / name))
        writable_root = working / "writable root"
        writable_root.mkdir()
        writable_root.chmod(0o770)
        rejected_paths.append((writable_root, writable_root))
        writable_bin = working / "writable bin" / "bin"
        writable_bin.mkdir(parents=True)
        writable_bin.chmod(0o702)
        rejected_paths.append((writable_bin.parent, writable_bin))
        linked_db = working / "linked database" / "data" / "taskboard.sqlite3"
        linked_db.parent.mkdir(parents=True)
        linked_db.symlink_to(sentinel)
        rejected_paths.append((linked_db.parents[1], linked_db))
        for candidate, unsafe_path in rejected_paths:
            entries = sorted(candidate.iterdir())
            mode = unsafe_path.lstat().st_mode
            rejected = install(SOURCE, install_root=candidate, expect_failure=True)
            assert rejected.returncode == 1 and f"Unsafe install path {unsafe_path}" in rejected.stderr
            assert sorted(candidate.iterdir()) == entries and unsafe_path.lstat().st_mode == mode
        assert list(external.iterdir()) == [sentinel] and sentinel.read_text() == "Preserve this file."
        with patch("install_local.os.geteuid", return_value=external.stat().st_uid + 1):
            try:
                check_existing_path(external, directory=True)
            except OSError as error:
                assert f"Unsafe install path {external}" in str(error)
            else:
                raise AssertionError("Installer accepted a path owned by a different user")

        install(SOURCE)
        first_build = (root / "current").read_text()
        assert not db.exists()
        assert "T-1 created" in tasks("add", "Check a sample export", "--dri", "Avery", "--step", "Read the ledger.")
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        with preview(launcher, working, env, port) as url:
            html = get(url)
            assert 'id="root"' in html
            asset = re.search(r'<script type="module"[^>]*src="([^"]+)"', html)
            assert asset and get(urljoin(url, asset[1]))
            original_ui = get(url + "api/v1/frontend-build", as_json=True)
            assert get(url + "api/v1/session", as_json=True)["actor"]["kind"] == "local"
            assert task(url)["state"] == "ready"
            assert "T-1 · Started" in tasks("start", "T-1", "Compare the saved sample.", "--by", "Avery")
            assert (task(url)["state"], task(url)["note"]) == ("in_progress", "Compare the saved sample.")
            request = Request(url + "api/v1/tasks/1/watch", data=b'{"watch":true}',
                              headers={"Content-Type": "application/json"})
            try:
                with loopback.open(request, timeout=2):
                    raise AssertionError("Local preview accepted a write")
            except HTTPError as error:
                with error:
                    assert error.code == 403

        with preview(launcher, working, env, port) as url:
            assert task(url)["note"] == "Compare the saved sample."
            assert get(url + "api/v1/frontend-build", as_json=True) == original_ui

        successor = working / "approved source fixture"
        successor.mkdir()
        copy_source(SOURCE, successor)
        (successor / "tools").mkdir()
        for name in ("install_local.py", "taskboard-local.sh"):
            shutil.copy2(SOURCE / "tools" / name, successor / "tools" / name)
        index = successor / "web" / "index.html"
        changed = index.read_text().replace("</title>", " · Upgrade fixture</title>")
        assert "Upgrade fixture" in changed
        index.write_text(changed)

        before_install = db.read_bytes()
        package_file = successor / "web" / "package.json"
        package = package_file.read_text()
        failed_package = json.loads(package)
        failed_package["scripts"]["build"] = 'node -e "process.exit(62)"'
        package_file.write_text(json.dumps(failed_package))
        failure = install(successor, expect_failure=True)
        assert failure.returncode == 1 and "exit status 62" in failure.stderr
        assert (root / "current").read_text() == first_build
        assert db.read_bytes() == before_install
        assert "Compare the saved sample." in tasks("show", "T-1")
        package_file.write_text(package)

        install(successor)
        assert (root / "current").read_text() != first_build
        assert db.read_bytes() == before_install
        assert "Compare the saved sample." in tasks("show", "T-1")
        with preview(launcher, working, env, port) as url:
            assert "Upgrade fixture</title>" in get(url)
            assert get(url + "api/v1/frontend-build", as_json=True) != original_ui
            assert (task(url)["key"], task(url)["dri"], task(url)["note"]) == ("T-1", "Avery", "Compare the saved sample.")
            assert "T-1 · Completed" in tasks("done", "T-1", "The sample matches.", "--by", "Avery")
            assert (task(url)["state"], task(url)["note"]) == ("done", "The sample matches.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use cached dependencies only.")
    check(offline=parser.parse_args().offline)
    print("Path validation, fresh install, preview restart, failed upgrade, and ledger-preserving upgrade passed.")
