"""Keep an enrolled notification unit on the same artifact as the serving slot."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryFile
import time

from release_plan import SlotCommandFailed
from release_slots import ReleaseSlots


_NOT_READY = {"starting", "failed", "state_locked"}
_STOPPED = {"grant_stopped", "subscription_stopped"}


def _process_environment(pid):
    return Path(f"/proc/{pid}/environ").read_bytes()


def _slot_credential(layout, settings, keys, *, run, read_environment):
    if (not isinstance(keys, dict) or keys.keys() != {"client_env", "secret_env"}
            or not all(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in keys.values())
            or len(set(keys.values())) != 2):
        raise ValueError("Configure distinct selected-slot app credential references.")
    slots = ReleaseSlots(settings, layout.database, run=run)
    release = layout.current.resolve(strict=True)
    port = slots.selected()
    pid = slots.pinned_pid(port, release)
    client_key, secret_key = (keys[field].encode("ascii") for field in ("client_env", "secret_env"))
    values = {}
    for entry in read_environment(pid).split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if name in (client_key, secret_key):
            if name in values or not separator or not value:
                raise ValueError("The selected slot app credential is malformed.")
            values[name] = value
    if values.keys() != {client_key, secret_key}:
        raise ValueError("The selected slot app credential is missing.")
    if slots.selected() != port or layout.current.resolve(strict=True) != release or slots.pinned_pid(port, release) != pid:
        raise ValueError("The serving slot changed during notification launch.")
    client = values[client_key].decode("ascii")
    if ":" in client or not client.isprintable():
        raise ValueError("The selected slot app client identifier is malformed.")
    return client, values[secret_key]


def exec_notification_worker(config, layout, scope_config, status, *, exec_worker=os.execv,
                             run=subprocess.run, read_environment=_process_environment):
    python = config.get("slots", {}).get("python", config.get("python", sys.executable))
    command = [python, "-B", str(layout.current / "tasks_managed_ready.py"), "run-service",
               "--config", str(scope_config), "--status", str(status)]
    if "notification_slot_credential" not in config:
        exec_worker(python, command)
        return
    client, secret = _slot_credential(layout, config["slots"], config["notification_slot_credential"],
                                      run=run, read_environment=read_environment)
    with TemporaryFile() as credential:
        credential.write(secret + b"\n")
        credential.seek(0)
        fd = credential.fileno()
        os.set_inheritable(fd, True)
        exec_worker(python, [*command, "--slot-credential", client, str(fd)])


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
