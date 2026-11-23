"""Invoke actions through the registered Raft app."""

import json
import re
from shlex import quote
import subprocess

from . import references as projects


BEFORE_DISPATCH = {"INTEGRATION_NOT_FOUND", "INTEGRATION_ACTION_NOT_FOUND", "INVALID_ARG"}
APP_REJECTION = {400, 401, 403, 404, 405, 409, 415, 422}
TEMPORARY_READ_STATUS = {502, 503, 504}
READ_FAILURE = "Shared read failed. Check the registered app and your Raft session."


def reply_json(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def exact_person_id(people, value, kind):
    name = value.removeprefix("@").casefold()
    people = [person for person in people if person["kind"] == kind]
    for field in ("id", "username", "name"):
        matches = [person["id"] for person in people if person.get(field, "").casefold() == name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            break
    raise ValueError(f"Choose one signed-in {kind} by exact handle or stable ID: {value}.")


class SharedActionUnavailable(RuntimeError):
    """The registered app does not advertise this action."""


class SharedReadError(RuntimeError):
    """A failed shared read with a safe reason for watcher diagnostics."""

    def __init__(self, message, *, reason="unclassified", retryable=False, carrier=None):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable
        self.carrier = carrier


def action_transport_timed_out(reply):
    error = reply.get("error")
    if not isinstance(error, dict):
        return False
    details = error.get("details")
    return (
        error.get("code") == "INTEGRATION_INVOKE_FAILED"
        and error.get("fault_domain") == "integration_action_transport"
        and error.get("retryable") is True
        and isinstance(details, dict)
        and details.get("cause_class") == "timeout"
    )


def carrier_error(result, reply):
    failure = reply.get("error", {})
    if not isinstance(failure, dict):
        failure = {}
    code = failure.get("code")
    if not isinstance(code, str) or not code:
        match = re.search(r"^Code: ([A-Z_]+)$", result.stderr, re.MULTILINE)
        code = match[1] if match else None
    return failure, code


def carrier_diagnostic(result, reply, failure, code):
    if isinstance(reply.get("error"), dict):
        output = "error-envelope"
    elif reply:
        output = "unexpected-envelope"
    elif code:
        output = "legacy-error"
    elif result.stdout.strip() or result.stderr.strip():
        output = "unstructured"
    else:
        output = "empty"
    diagnostic = {"exit_code": result.returncode, "output": output}
    if code:
        known = code in BEFORE_DISPATCH or code == "INTEGRATION_INVOKE_FAILED"
        diagnostic["error_code"] = code if known else "other"
    domain = failure.get("fault_domain")
    if domain in ("integration_action", "integration_action_transport"):
        diagnostic["fault_domain"] = domain
    return diagnostic


def invocation_failure(result, reply, failure, code, service, action):
    message = failure.get("message", "")
    details = failure.get("details", {})
    if not isinstance(details, dict):
        details = {}
    if code == "INTEGRATION_NOT_FOUND":
        return ("The Tasks app is missing from your Raft integration inventory. Check `raft integration list`.",
                None, "raft-integration-missing")
    missing_action = f"manifest does not define action {action}"
    if code == "INTEGRATION_ACTION_NOT_FOUND" or (
        code == "INVALID_ARG" and (message == missing_action or f"Error: {missing_action}" in result.stderr.splitlines())
    ):
        raise SharedActionUnavailable(
            f"The registered Tasks app does not offer this action. Check `raft integration invoke --service "
            f"{quote(service)} --list-actions`."
        )
    if code in BEFORE_DISPATCH:
        reason = (
            "Raft rejected the invocation before dispatch. "
            f"Check the supported actions with `raft integration invoke --service {quote(service)} --list-actions`."
        )
        return reason, None, "raft-before-dispatch"
    status_text, _, embedded_body = str(message if failure else result.stderr).partition("; response body: ")
    match = re.search(r"\bHTTP (\d{3})\b", status_text)
    status = int(match[1]) if code == "INTEGRATION_INVOKE_FAILED" and match else None
    if status in TEMPORARY_READ_STATUS:
        header = status_text.strip().splitlines()[0].removeprefix("Error: ")
        if header != f"service action failed (HTTP {status})":
            status = None
    if status in APP_REJECTION:
        if status == 401:
            reason = (
                "The Tasks app did not accept the current Raft session. "
                f"Run `raft integration login --service {quote(service)}`, then verify the account and board "
                "with `./tasks --shared context`."
            )
            return reason, status, None
        body = details.get("response_body", embedded_body)
        if isinstance(body, str):
            body = reply_json(body)
        explanation = body.get("error") if isinstance(body, dict) else None
        if isinstance(explanation, dict):
            explanation = explanation.get("message")
        if isinstance(explanation, str) and explanation.strip():
            return " ".join(explanation.split())[:300], status, None
        return f"The Tasks app rejected the action (HTTP {status}).", status, None
    if status is not None:
        return None, status, None
    if code == "INTEGRATION_INVOKE_FAILED":
        category = ("raft-action-transport" if failure.get("fault_domain") == "integration_action_transport"
                    else "raft-invocation-failed")
    elif not reply and code is None:
        category = "carrier-no-reply"
    else:
        category = "unclassified"
    return None, None, category


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
            if write else READ_FAILURE
        )
        if action == "save-goal":
            unknown = "Shared write outcome unknown. Read your project goals before retrying."
        try:
            result = subprocess.run(call, input=payload, capture_output=True, text=True, timeout=45)
        except FileNotFoundError as error:
            reason = "Raft CLI is required for shared mode."
            if not write:
                raise SharedReadError(reason, reason="raft-unavailable") from error
            raise RuntimeError(reason) from error
        except subprocess.TimeoutExpired as error:
            if not write:
                raise SharedReadError(unknown, reason="timeout", retryable=True) from error
            raise RuntimeError(unknown) from error
        except KeyboardInterrupt as error:
            if not write:
                raise SharedReadError(unknown, reason="interrupted") from error
            raise RuntimeError(unknown) from error
        reply = reply_json(result.stdout)
        if result.returncode != 0 and not reply:
            failure = reply_json(result.stderr)
            if failure.get("ok") is False:
                reply = failure
        if result.returncode != 0 or reply.get("ok") is not True:
            failure, code = carrier_error(result, reply)
            if not write and action_transport_timed_out(reply):
                raise SharedReadError(
                    unknown, reason="timeout", retryable=True,
                    carrier=carrier_diagnostic(result, reply, failure, code),
                )
            reason, status, category = invocation_failure(result, reply, failure, code, self.service, action)
            if write:
                raise RuntimeError(f"Shared write rejected. {reason}" if reason else unknown)
            raise SharedReadError(
                f"Shared read failed. {reason}" if reason else unknown,
                reason=f"http-{status}" if status is not None else category,
                retryable=status in TEMPORARY_READ_STATUS,
                carrier=carrier_diagnostic(result, reply, failure, code),
            )
        envelope = reply.get("data")
        response = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(response, dict):
            if not write:
                raise SharedReadError(
                    unknown, reason="invalid-reply",
                    carrier=carrier_diagnostic(result, reply, {}, None),
                )
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
        return exact_person_id(self.pages("assignees", filters), value, kind)
