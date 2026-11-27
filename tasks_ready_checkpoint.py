#!/usr/bin/env python3
"""Launch taskboard_cli.ready_checkpoint from this checkout."""

import signal
import subprocess
import sys
from pathlib import Path

from tools.checkout import add_packages

add_packages("client", "cli")

from taskboard_cli.ready_checkpoint import MANAGED_RESTART, main


def supervise(command):
    child = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.returncode is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for attempt in (1, 2):
            if stopping:
                return 0
            child = subprocess.Popen(command)
            if stopping:
                child.terminate()
            status = child.wait()
            if stopping:
                return 0
            if status != MANAGED_RESTART:
                return status if status >= 0 else 128 - status
            if attempt == 1:
                print("ready checkpoint: restarting once after the failed recovery alert", flush=True)
    except OSError as error:
        print(f"ready checkpoint: could not run the managed child: {error.strerror}", file=sys.stderr)
        return 1
    print("ready checkpoint: managed restart exhausted", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--restart-once"]:
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--managed-child", *sys.argv[2:]]
        raise SystemExit(supervise(command))
    managed_child = sys.argv[1:2] == ["--managed-child"]
    if managed_child:
        del sys.argv[1]
    raise SystemExit(main(managed_child=managed_child))
