"""Persistent project creation grants for verified Taskboard principals."""

SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_admin_grants (
        principal TEXT PRIMARY KEY REFERENCES board_principals (ledger_key)
    )
"""


def eligible(actor):
    return actor["kind"] in ("human", "agent")


def manager(actor, subject):
    return subject is not None and actor["kind"] == "human" and actor["subject"] == subject


def allowed(db, actor, subject):
    return subject is not None and eligible(actor) and (
        manager(actor, subject) or db.execute(
            "SELECT 1 FROM project_admin_grants WHERE principal = ?", (actor["ledger_key"],),
        ).fetchone() is not None
    )


def directory(db, server, subject):
    rows = db.execute(
        """
        SELECT principal.ledger_key, principal.subject, principal.kind, principal.name, principal.username,
               grants.principal AS granted
        FROM board_principals AS principal
        LEFT JOIN project_admin_grants AS grants ON grants.principal = principal.ledger_key
        WHERE principal.server_id = ?
        ORDER BY LOWER(principal.name), principal.ledger_key
        """, (server,),
    )
    items = []
    for row in rows:
        if eligible(row):
            fixed = manager(row, subject)
            items.append({
                "id": row["ledger_key"], "name": row["name"] or row["username"] or row["ledger_key"],
                "handle": row["username"] or None,
                "kind": row["kind"], "can_create_project": fixed or row["granted"] is not None,
                "managed": not fixed,
            })
    return items


def registered_principals(db, server):
    return db.execute(
        """
        SELECT principal.ledger_key, principal.subject, principal.kind, principal.name, principal.username,
               local.project AS local_project, grants.principal AS project_creation_grant
        FROM board_principals AS principal
        LEFT JOIN local_dris AS local ON local.actor = principal.ledger_key
        LEFT JOIN project_admin_grants AS grants ON grants.principal = principal.ledger_key
        WHERE principal.server_id = ? AND principal.kind IN ('human', 'agent')
        ORDER BY LOWER(principal.name), principal.ledger_key
        """, (server,),
    )


def registration(row):
    return {
        "id": row["ledger_key"],
        "name": row["name"] or row["username"] or row["ledger_key"],
        "handle": row["username"] or None,
        "kind": "local_dri" if row["local_project"] else "team_dri" if row["kind"] == "agent" else "human",
    }


def set_grant(db, principal, enabled):
    if enabled:
        return db.execute(
            "INSERT INTO project_admin_grants VALUES (?) ON CONFLICT (principal) DO NOTHING", (principal,),
        ).rowcount > 0
    return db.execute("DELETE FROM project_admin_grants WHERE principal = ?", (principal,)).rowcount > 0
