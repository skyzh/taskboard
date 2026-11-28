"""Keep an enrolled notification unit on the same artifact as the serving slot."""

import json
import os
from pathlib import Path
import stat
import subprocess
import time

from release_plan import SlotCommandFailed


_NOT_READY = {"starting", "failed", "state_locked"}
_STOPPED = {"grant_stopped", "subscription_stopped"}


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Use an operator-owned private notification file.")
        return json.load(source)


def _roster(scopes):
    identities = {(scope["project"], scope["recipient"]) for scope in scopes}
    if not identities or len(identities) != len(scopes):
        raise ValueError("Configure each notification scope only once.")
    return identities


def _matches(path, expected, pid, enrollment):
    try:
        data = _private_json(path)
        scopes = data["scopes"]
        if (data["manifest"] != expected or data["pid"] != pid
                or data["enrolled"] != len(enrollment) or _roster(scopes) != enrollment):
            return False
        states = [scope["status"] for scope in scopes]
        return not _NOT_READY.intersection(states) and data["active"] == sum(state not in _STOPPED for state in states)
    except (OSError, KeyError, ValueError, TypeError):
        return False


def checked_worker(healthy, unit, status, scope_config, restart, *, run=subprocess.run, pause=time.sleep):
    path = Path(status)
    scope_config = Path(scope_config)
    if not path.is_absolute() or not scope_config.is_absolute():
        raise ValueError("Set absolute notification scope and private status file paths.")
    enrollment = _roster(_private_json(scope_config)["scopes"])

    def running(expected):
        result = run(["systemctl", "--user", "show", "--value", "--property=MainPID", unit],
                     capture_output=True, text=True, check=True, timeout=5)
        pid = int(result.stdout.strip())
        return pid > 0 and _matches(path, expected, pid, enrollment)

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
