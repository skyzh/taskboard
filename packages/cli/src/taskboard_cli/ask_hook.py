"""Run an explicitly configured local checker before a signed ask is sent."""

import json
import os
from subprocess import TimeoutExpired, run


COMMAND = "TASKBOARD_PRE_SEND_ASK"
TIMEOUT = "TASKBOARD_PRE_SEND_ASK_TIMEOUT"
DRAFT_SCHEMA = "taskboard.pre-send-ask.v1"
RESULT_SCHEMA = "taskboard.pre-send-ask-result.v1"
PROCESS_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
               "SystemRoot", "SystemDrive", "WINDIR", "USERPROFILE")


def check_ask(kind, ticket, note, **context):
    configured = os.environ.get(COMMAND)
    if not configured:
        return
    try:
        command = json.loads(configured)
    except ValueError:
        raise ValueError(f"Ask not sent. Set {COMMAND} to a JSON array of command arguments.") from None
    if (not isinstance(command, list) or not command or not all(isinstance(argument, str) for argument in command)
            or not command[0].strip() or any("\0" in argument for argument in command)):
        raise ValueError(f"Ask not sent. Set {COMMAND} to a JSON array of command arguments.")
    try:
        timeout = int(os.environ.get(TIMEOUT, "30"))
    except ValueError:
        timeout = 0
    if not 1 <= timeout <= 300:
        raise ValueError(f"Ask not sent. Set {TIMEOUT} to 1–300 seconds.")
    draft = {
        "schema": DRAFT_SCHEMA,
        "ask": {"kind": kind, "ticket": ticket, "note": note,
                **{key: value for key, value in context.items() if value is not None}},
    }
    environment = {key: os.environ[key] for key in PROCESS_ENV if key in os.environ}
    try:
        result = run(
            command, input=json.dumps(draft, ensure_ascii=False), encoding="utf-8",
            capture_output=True, timeout=timeout, env=environment,
        )
    except TimeoutExpired:
        raise ValueError(f"Ask not sent. The pre-send checker timed out after {timeout} seconds.") from None
    except (OSError, UnicodeError):
        raise ValueError("Ask not sent. The pre-send checker could not run or returned invalid text.") from None
    if result.returncode:
        raise ValueError(f"Ask not sent. The pre-send checker exited with status {result.returncode}.")
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        reply = None
    if isinstance(reply, dict) and reply.get("schema") == RESULT_SCHEMA:
        if reply.get("decision") == "pass":
            return
        if reply.get("decision") == "revise" and isinstance(reply.get("feedback"), str):
            feedback = "".join(character for character in reply["feedback"] if character == "\n" or character.isprintable())
            if feedback.strip():
                raise ValueError(f"Ask not sent. The pre-send checker requests revisions:\n{feedback.strip()[:2000]}")
    raise ValueError(f"Ask not sent. The pre-send checker must return a valid {RESULT_SCHEMA} response.")
