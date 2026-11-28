"""Keep an enrolled notification unit on the same artifact as the serving slot."""

import json
import os
from pathlib import Path
import stat
import subprocess
import time

from release_plan import SlotCommandFailed


_NOT_READY = {"starting", "failed", "state_locked"}


def _matches(path, expected, pid):
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        scopes = data["scopes"]
        if data["manifest"] != expected or data["pid"] != pid or data["enrolled"] != len(scopes) or not scopes:
            return False
        identities = {(scope["project"], scope["recipient"]) for scope in scopes}
        return len(identities) == len(scopes) and all(scope["status"] not in _NOT_READY for scope in scopes)
    except (OSError, KeyError, ValueError, TypeError):
        return False


def checked_worker(healthy, unit, status, restart, *, run=subprocess.run, pause=time.sleep):
    path = Path(status)
    if not path.is_absolute():
        raise ValueError("Set the private notification status file’s absolute path.")

    def running(expected):
        result = run(["systemctl", "--user", "show", "--value", "--property=MainPID", unit],
                     capture_output=True, text=True, check=True, timeout=5)
        pid = int(result.stdout.strip())
        return pid > 0 and _matches(path, expected, pid)

    def checked(expected):
        if not healthy(expected):
            return False
        try:
            if running(expected):
                return True
            restart()
            for _ in range(20):
                if running(expected):
                    return True
                pause(1)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise SlotCommandFailed("notification_worker", "service_command_failed") from error
        raise SlotCommandFailed("notification_worker", "revision_or_scope_unconfirmed")

    return checked
