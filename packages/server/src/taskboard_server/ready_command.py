"""Deliver a qualified Ready cue through one explicitly trusted local command."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess


SCHEMA = "taskboard.ready-command.v1"
_TIMEOUT = 5
_MAX_OUTPUT = 4096


@dataclass(frozen=True)
class ReadyCommand:
    argv: tuple[str, ...]

    @classmethod
    def parse(cls, value):
        try:
            args = json.loads(value)
        except ValueError:
            raise ValueError("Configure the delivery command as a JSON argv array.") from None
        if (not isinstance(args, list) or not 0 < len(args) <= 32
                or any(not isinstance(arg, str) or len(arg) > 4096 or "\0" in arg for arg in args)
                or not Path(args[0]).is_absolute()):
            raise ValueError("Choose a bounded JSON argv array starting with an absolute executable.")
        return cls(tuple(args))

    @property
    def binding(self):
        return "command:" + sha256(json.dumps(self.argv).encode()).hexdigest()

    def __call__(self, cue):
        try:
            result = subprocess.run(
                self.argv, input=json.dumps({"schema": SCHEMA, **cue}).encode() + b"\n",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, timeout=_TIMEOUT, check=False,
            )
            if result.returncode or len(result.stdout) > _MAX_OUTPUT:
                return "unknown"
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "unknown"
        if (not isinstance(response, dict) or response.keys() != {"external_event_id", "status"}
                or response["external_event_id"] != cue["external_event_id"]
                or response["status"] not in ("accepted", "rejected", "unknown")):
            return "unknown"
        return response["status"]
