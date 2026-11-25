"""Project-defined ticket fields and their evidence-backed Activity history."""

from datetime import datetime
import json
import re

from tasks_agent_usage import AMOUNT, CURRENCY


KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
KINDS = {"duration", "money", "decimal", "text"}
METHODS = {"measured", "estimated", "unknown"}
COVERAGE = {"complete", "partial"}
ACTION = "set-ticket-field"
SCHEMA = """
    CREATE TABLE IF NOT EXISTS ticket_field_receipts (
        event_id INTEGER PRIMARY KEY REFERENCES events (id),
        value TEXT NOT NULL,
        links TEXT NOT NULL
    )
"""


def line(value, maximum, field):
    if not isinstance(value, str) or not value.isprintable() or not 0 < len(value.strip()) <= maximum:
        raise ValueError(f"Provide {field} in one line of at most {maximum} characters.")
    return value.strip()


def validate_definitions(fields):
    if not isinstance(fields, list) or len(fields) > 12:
        raise ValueError("Provide up to 12 project ticket fields.")
    definitions = {}
    for field in fields:
        if (not isinstance(field, dict) or not {"key", "label", "kind", "prompt"} <= field.keys()
                or field.keys() - {"key", "label", "kind", "prompt", "source"}):
            raise ValueError("Each field needs a key, label, kind, and collection prompt.")
        key = line(field["key"], 32, "a field key")
        kind = line(field["kind"], 16, "a field kind")
        if not KEY.fullmatch(key) or key in definitions:
            raise ValueError("Use unique lowercase field keys beginning with a letter.")
        if kind not in KINDS:
            raise ValueError("Choose duration, money, decimal, or text.")
        item = {"key": key, "kind": kind, "label": line(field["label"], 64, "a label"),
                "prompt": line(field["prompt"], 500, "a collection prompt")}
        if "source" in field:
            if field["source"] != "recorded_workers" or kind != "duration":
                raise ValueError("Recorded workers can supply a duration field.")
            item["source"] = "recorded_workers"
        definitions[key] = item
    return list(definitions.values())


def definitions(db, project):
    row = db.execute("SELECT value FROM board_settings WHERE key = ?", (f"ticket-fields:{project}",)).fetchone()
    return json.loads(row["value"]) if row is not None else []


def save_definitions(db, project, fields):
    fields = validate_definitions(fields)
    if definitions(db, project) == fields:
        return False
    db.execute(
        "INSERT INTO board_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (f"ticket-fields:{project}", json.dumps(fields)),
    )
    return True


def validate_value(field, value, method, coverage, currency, basis):
    kind = field["kind"]
    if method not in METHODS:
        raise ValueError("Choose measured, estimated, or unknown.")
    basis = line(basis, 240, "a source or estimation basis")
    if method == "unknown":
        if value is not None or coverage is not None or currency is not None:
            raise ValueError("An unknown field takes a basis only.")
    else:
        if coverage not in COVERAGE:
            raise ValueError("Choose complete or partial coverage.")
        value = line(value, 500 if kind == "text" else 22, "a field value")
        if kind == "duration" and not re.fullmatch(r"[0-9]{1,12}", value):
            raise ValueError("Record duration in whole seconds.")
        if kind in {"money", "decimal"} and not AMOUNT.fullmatch(value):
            raise ValueError("Record a nonnegative decimal amount with up to eight decimal places.")
        if kind == "money" and (currency is None or not CURRENCY.fullmatch(currency)):
            raise ValueError("Record a three-letter uppercase currency code.")
        if kind != "money" and currency is not None:
            raise ValueError("Supply currency only for money fields.")
    return {"key": field["key"], "kind": kind, "value": value, "method": method,
            "coverage": coverage, "currency": currency, "basis": basis}


def saved_values(db, number, *, event_id=None):
    rows = db.execute(
        """
        SELECT events.id, events.actor, events.at, receipt.value, receipt.links
        FROM events JOIN ticket_field_receipts AS receipt ON receipt.event_id = events.id
        WHERE events.task_id = ? AND (CAST(? AS BIGINT) IS NULL OR events.id = ?) ORDER BY events.id DESC
        """, (number, event_id, event_id),
    )
    values = {}
    for row in rows:
        value = json.loads(row["value"])
        values.setdefault((value["key"], value["kind"]), {
            **value, "event_id": row["id"], "actor": row["actor"], "at": row["at"], "links": json.loads(row["links"]),
        })
    return values


def save_receipt(db, event, value, links):
    db.execute("INSERT INTO ticket_field_receipts VALUES (?, ?, ?)", (event, json.dumps(value), json.dumps(links)))


def worker_duration(runs):
    seconds = 0
    counted = 0
    for run in runs:
        ending = run["reported_at"] or (run["finalized_at"] if run["status"] == "stopped" else None)
        if not (run["started_at"] and run["handle"] and ending):
            continue
        try:
            start = datetime.fromisoformat(run["started_at"])
            end = datetime.fromisoformat(ending)
        except ValueError:
            continue
        if start.tzinfo is not None and end.tzinfo is not None and end >= start:
            seconds += int((end - start).total_seconds())
            counted += 1
    if not counted:
        return None
    return {"value": str(seconds), "method": "measured", "coverage": "partial", "currency": None,
            "basis": f"Recorded elapsed time from {counted} ticket-linked worker runs. Direct agent work is unavailable."}


def detail(db, number, fields, runs, *, editable):
    values = saved_values(db, number)
    result = []
    for definition in fields:
        value = values.get((definition["key"], definition["kind"]))
        if value is None and definition.get("source") == "recorded_workers":
            value = worker_duration(runs)
        result.append({**definition, **(value or {"value": None, "method": "unknown", "coverage": None, "currency": None})})
    return {"editable": editable, "fields": result}
