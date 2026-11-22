"""Invoke actions through the registered Raft app."""

import json
import re
from shlex import quote
import subprocess

from . import references as projects


BEFORE_DISPATCH = {"INTEGRATION_NOT_FOUND", "INTEGRATION_ACTION_NOT_FOUND", "INVALID_ARG"}
APP_REJECTION = {400, 401, 403, 404, 405, 409, 415, 422}


def reply_json(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class SharedActionUnavailable(RuntimeError):
    """The registered app does not advertise this action."""


def invocation_error(result, reply, service, action):
    failure = reply.get("error", {})
    if not isinstance(failure, dict):
        failure = {}
    message = failure.get("message", "")
    details = failure.get("details", {})
    if not isinstance(details, dict):
        details = {}
    code = failure.get("code")
    if not code:
        code_match = re.search(r"^Code: ([A-Z_]+)$", result.stderr, re.MULTILINE)
        code = code_match[1] if code_match else None
    if code == "INTEGRATION_NOT_FOUND":
        return "The Tasks app is missing from your Raft integration inventory. Check `raft integration list`."
    missing_action = f"manifest does not define action {action}"
    if code == "INTEGRATION_ACTION_NOT_FOUND" or (
        code == "INVALID_ARG" and (message == missing_action or f"Error: {missing_action}" in result.stderr.splitlines())
    ):
        raise SharedActionUnavailable(
            f"The registered Tasks app does not offer this action. Check `raft integration invoke --service "
            f"{quote(service)} --list-actions`."
        )
    if code in BEFORE_DISPATCH:
        return (
            "Raft rejected the invocation before dispatch. "
            f"Check the supported actions with `raft integration invoke --service {quote(service)} --list-actions`."
        )
    status_text, _, embedded_body = str(message if failure else result.stderr).partition("; response body: ")
    status = re.search(r"\bHTTP (\d{3})\b", status_text)
    if code == "INTEGRATION_INVOKE_FAILED" and status and int(status[1]) in APP_REJECTION:
        if status[1] == "401":
            return (
                "The Tasks app did not accept the current Raft session. "
                f"Run `raft integration login --service {quote(service)}`, then verify the account and board "
                "with `./tasks --shared context`."
            )
        body = details.get("response_body", embedded_body)
        if isinstance(body, str):
            body = reply_json(body)
        explanation = body.get("error") if isinstance(body, dict) else None
        if isinstance(explanation, dict):
            explanation = explanation.get("message")
        if isinstance(explanation, str) and explanation.strip():
            return " ".join(explanation.split())[:300]
        return f"The Tasks app rejected the action (HTTP {status[1]})."
    return None


class SharedBoard:
    def __init__(self, service):
        self.service = service

    def invoke(self, action, params=None, *, write=False):
        call = ["raft", "integration", "invoke", "--service", self.service, "--action", action, "--json"]
        payload = None
        if write:
            call += ["--data-file", "-"]
            payload = json.dumps(params or {})
        else:
            for key, value in (params or {}).items():
                call += ["--param", f"{key}={value}"]
        unknown = (
            "Shared write outcome unknown. Read the task or your queue before retrying."
            if write else "Shared read failed. Check the registered app and your Raft session."
        )
        if action == "save-goal":
            unknown = "Shared write outcome unknown. Read your project goals before retrying."
        try:
            result = subprocess.run(call, input=payload, capture_output=True, text=True, timeout=45)
        except FileNotFoundError as error:
            raise RuntimeError("Raft CLI is required for shared mode.") from error
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            raise RuntimeError(unknown) from error
        reply = reply_json(result.stdout)
        if result.returncode != 0 and not reply:
            failure = reply_json(result.stderr)
            if failure.get("ok") is False:
                reply = failure
        if result.returncode != 0 or reply.get("ok") is not True:
            reason = invocation_error(result, reply, self.service, action)
            if reason:
                raise RuntimeError(f"Shared {'write rejected' if write else 'read failed'}. {reason}")
            raise RuntimeError(unknown)
        envelope = reply.get("data")
        response = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(response, dict):
            raise RuntimeError(unknown)
        if write:
            receipt = response.get("receipt")
            if not isinstance(receipt, str) or not receipt.strip():
                raise RuntimeError(unknown)
        return response

    def pages(self, action, params=None):
        fields = dict(params or {})
        while True:
            page = self.invoke(action, fields)
            yield from page["items"]
            if page["next_cursor"] is None:
                return
            fields["before"] = page["next_cursor"]

    def person(self, value, kind, task=None):
        if value.startswith(f"raft:{kind}:"):
            return value
        name = value.removeprefix("@")
        filters = {"query": name}
        if task is not None:
            project, _ = projects.identifier(task)
            if project != projects.DEFAULT:
                filters["project"] = project
        people = [person for person in self.pages("assignees", filters) if person["kind"] == kind]
        for field in ("id", "username", "name"):
            matches = [person["id"] for person in people if person.get(field, "").casefold() == name.casefold()]
            if len(matches) == 1:
                return matches[0]
            if matches:
                break
        raise ValueError(f"Choose one signed-in {kind} by exact handle or stable ID: {value}.")
