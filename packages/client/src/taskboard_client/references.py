"""Read public Taskboard project and ticket identifiers."""

import re


DEFAULT = "T"
MAX_ID = 2**63 - 1
_PREFIX = re.compile(r"[A-Z][A-Z0-9]{0,7}", re.ASCII)
_TASK = re.compile(r"([A-Z][A-Z0-9]{0,7})-([0-9]{1,19})", re.ASCII)


def prefix(value):
    value = value.upper()
    if _PREFIX.fullmatch(value) is None:
        raise ValueError("Use a project prefix of one to eight letters or digits, starting with a letter.")
    return value


def identifier(value):
    value = str(value).upper()
    matched = _TASK.fullmatch(value)
    project, digits = matched.groups() if matched else (DEFAULT, value)
    if len(digits) <= 19 and digits.isascii() and digits.isdecimal():
        number = int(digits)
        if 1 <= number <= MAX_ID:
            return (project, number)
    raise ValueError("Use a task ID such as T-1 or COURSE-1.")
