"""Manage two pinned Taskboard user services behind the portal's atomic selector."""

from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import re
import subprocess
from tempfile import NamedTemporaryFile
import time

from release_compatibility import verify_rollback_ledger
from release_plan import MountedCheckFailed, ReleaseRejected, SlotCommandFailed, verify_manifest


_UNIT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ADMISSION_HEADER = "X-Portal-Upstream-Token"
_SLOT_PROBE_ATTEMPTS = 8
# A measured Pi cold start took up to 26 seconds to answer both authenticated probes.
_CANDIDATE_STARTUP_ATTEMPTS = 32
_SERVING_POLICY = (
    "--property=Restart=on-failure",
    "--property=RestartSec=5s",
    "--property=StartLimitIntervalSec=2min",
    "--property=StartLimitBurst=5",
    "--property=StartLimitAction=none",
    "--property=TimeoutStopSec=15s",
)


class ReleaseSlots:
    def __init__(self, settings, ledger, *, run=subprocess.run, pause=time.sleep):
        self.selector = Path(settings["selector"])
        self.environment = Path(settings["environment_file"])
        self.admission = Path(settings["admission_file"])
        self.python = Path(settings["python"])
        self.ledger = ledger
        self.mount_path = settings["mount_path"]
        self.units = {int(port): unit for port, unit in settings["units"].items()}
        if (
            len(self.units) != 2 or any(not 1024 <= port <= 65535 for port in self.units)
            or not all(isinstance(unit, str) and _UNIT.fullmatch(unit) for unit in self.units.values())
            or len(set(self.units.values())) != 2
            or not all(path.is_absolute() for path in (self.selector, self.environment, self.admission, self.python))
            or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", self.mount_path)
        ):
            raise ValueError("Configure two Taskboard loopback ports, user units, and absolute slot paths.")
        self.run = run
        self.pause = pause

    def selected(self):
        text = self.selector.read_text(encoding="ascii")
        if text not in {f"{port}\n" for port in self.units}:
            raise ReleaseRejected("Reconcile the Taskboard port selector with the configured slots.", "slot_selector")
        return int(text)

    def plan(self, previous):
        selected = self.selected()
        self.verify(selected, previous)
        self._record_release(selected, previous)
        inactive = next(port for port in self.units if port != selected)
        return [selected, inactive]

    @staticmethod
    def _replace_line(path, value):
        with NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(f"{value}\n")
        os.replace(temporary, path)

    def _release_file(self, port):
        return self.selector.with_name(f"{self.selector.name}.{port}.release")

    def _record_release(self, port, release):
        self._replace_line(self._release_file(port), release.name)

    def select(self, port):
        if port not in self.units:
            raise ValueError("Use a configured Taskboard loopback port.")
        self._replace_line(self.selector, port)

    def _systemctl(self, *arguments):
        return self.run(["systemctl", "--user", *arguments], check=True, capture_output=True, text=True, timeout=45)

    def prepare(self, port, release, previous):
        if self.selected() == port:
            raise ReleaseRejected("The Taskboard candidate port must be inactive.", "slot_selector")
        verify_rollback_ledger(self.python, previous, release)
        self._launch(port, release, attempts=_CANDIDATE_STARTUP_ATTEMPTS)
        self._record_release(port, release)

    def restore_selected(self, layout):
        port = self.selected()
        pin = self._release_file(port)
        if pin.exists():
            digest = pin.read_text(encoding="ascii").removesuffix("\n")
            if not _DIGEST.fullmatch(digest):
                raise ReleaseRejected("Reconcile the selected Taskboard port's release pin.", "slot_selector")
            release = layout.releases / digest
        else:
            if any(self._release_file(slot).exists() for slot in self.units):
                raise ReleaseRejected("Restore the selected Taskboard port's release pin.", "slot_selector")
            release = layout.current.resolve(strict=True)
            digest = release.name
        if not _DIGEST.fullmatch(digest) or release.parent != layout.releases or release.resolve() != release:
            raise ReleaseRejected("Select a pinned release in the Taskboard release directory.", "slot_selector")
        verify_manifest(release, digest)
        self.ensure_running(port, release)

    def ensure_running(self, port, release):
        state = self._systemctl("show", self.units[port], "--property=ActiveState", "--value").stdout.strip()
        if state == "active":
            self.verify(port, release)
        else:
            self._launch(port, release)
        self._record_release(port, release)

    def _run_slot_command(self, stage, command):
        try:
            self.run(command, check=True, capture_output=True, text=True, timeout=45)
        except subprocess.CalledProcessError as error:
            raise SlotCommandFailed(stage, "nonzero_exit", returncode=error.returncode) from None
        except subprocess.TimeoutExpired:
            raise SlotCommandFailed(stage, "timeout") from None
        except OSError:
            raise SlotCommandFailed(stage, "execution_error") from None

    def _launch(self, port, release, *, attempts=_SLOT_PROBE_ATTEMPTS):
        unit = self.units[port]
        if self._systemctl("show", unit, "--property=LoadState", "--value").stdout.strip() != "not-found":
            self._run_slot_command("stop", ["systemctl", "--user", "stop", unit])
        self._run_slot_command("launch", [
            "systemd-run", "--user", "--collect", f"--unit={unit}", "--property=Type=exec",
            f"--property=WorkingDirectory={release}", f"--property=EnvironmentFile={self.environment}",
            *_SERVING_POLICY,
            self.python, "-B", release / "tasks", "--db", self.ledger, "serve",
            "--portal-reader", "--websocket-revisions", "--host", "127.0.0.1", "--port", str(port),
        ])
        self.verify(port, release, attempts=attempts)

    def pinned_pid(self, port, release):
        unit = self.units[port]
        properties = self._systemctl(
            "show", unit, "--property=WorkingDirectory", "--property=MainPID", "--property=ActiveState",
        ).stdout
        values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        if (
            values.get("WorkingDirectory") != str(release) or values.get("ActiveState") != "active"
            or not re.fullmatch(r"[1-9][0-9]*", values.get("MainPID", ""))
        ):
            raise MountedCheckFailed([{"name": "slot", "reason": "not_pinned_to_release"}])
        return int(values["MainPID"])

    def verify(self, port, release, *, attempts=_SLOT_PROBE_ATTEMPTS):
        self.pinned_pid(port, release)
        credential = self.admission.read_text(encoding="utf-8").strip()
        if not credential:
            raise MountedCheckFailed([{"name": "slot", "reason": "missing_portal_admission"}])
        for attempt in range(attempts):
            try:
                self._check_http(port, credential)
                return
            except (OSError, HTTPException):
                if attempt + 1 == attempts:
                    break
                self.pause(1)
        raise MountedCheckFailed([{"name": "slot", "reason": "loopback_unavailable"}])

    def _check_http(self, port, credential):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            for suffix in ("/healthz", "/api/v1/revision"):
                connection.request("GET", self.mount_path + suffix, headers={_ADMISSION_HEADER: credential})
                response = connection.getresponse()
                if response.status != 200:
                    if response.status in (502, 503, 504):
                        raise HTTPException()
                    raise MountedCheckFailed([{"name": "slot", "reason": f"http_{response.status}"}])
                response.read(4096)
        finally:
            connection.close()
