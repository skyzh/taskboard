"""Deliver a qualified Ready cue through one explicitly trusted local command."""

from dataclasses import dataclass
from hashlib import sha256
import json
from os import killpg, read
from pathlib import Path
from select import select
from signal import SIGKILL
import subprocess
from tempfile import TemporaryFile
from time import monotonic


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
        except (ValueError, RecursionError):
            raise ValueError("Configure the delivery command as a JSON argv array.") from None
        if (not isinstance(args, list) or not 0 < len(args) <= 32
                or any(not isinstance(arg, str) or len(arg) > 4096 or "\0" in arg for arg in args)
                or not Path(args[0]).is_absolute()):
            raise ValueError("Choose a bounded JSON argv array starting with an absolute executable.")
        return cls(tuple(args))

    @property
    def binding(self):
        return "command:" + sha256(json.dumps(self.argv).encode()).hexdigest()

    def _response(self, cue):
        with TemporaryFile() as stdin:
            stdin.write(json.dumps({"schema": SCHEMA, **cue}).encode() + b"\n")
            stdin.seek(0)
            deadline = monotonic() + _TIMEOUT
            with subprocess.Popen(
                self.argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True,
                start_new_session=True,
            ) as process:
                try:
                    output = bytearray()
                    while len(output) <= _MAX_OUTPUT:
                        remaining = deadline - monotonic()
                        if remaining <= 0 or not select([process.stdout], [], [], remaining)[0]:
                            return None
                        chunk = read(process.stdout.fileno(), _MAX_OUTPUT + 1 - len(output))
                        if not chunk:
                            if process.wait(timeout=max(0, deadline - monotonic())) == 0:
                                return json.loads(output)
                            return None
                        output.extend(chunk)
                    return None
                finally:
                    # Helpers must finish within the synchronous command attempt.
                    try:
                        killpg(process.pid, SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

    def __call__(self, cue):
        try:
            response = self._response(cue)
        except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
            return "unknown"
        if (not isinstance(response, dict) or response.keys() != {"external_event_id", "status"}
                or response["external_event_id"] != cue["external_event_id"]
                or response["status"] not in ("accepted", "rejected", "unknown")):
            return "unknown"
        return response["status"]
