"""Stable board identities and self-managed assignee profiles."""

from hashlib import sha256
from io import BytesIO
from uuid import UUID
from xml.etree import ElementTree

from PIL import Image, ImageOps

import tasks_storage


MAX_AVATAR_BYTES = 1024 * 1024
MAX_DESCRIPTION_LENGTH = 2000
MAX_SHORT_ROLE_LENGTH = 60
AVATAR_SIZE = 256
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SVG_ROOT = "{http://www.w3.org/2000/svg}svg"
SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_principals (
        server_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        ledger_key TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        picture TEXT,
        role TEXT,
        description TEXT,
        avatar BLOB,
        avatar_digest TEXT,
        provider_description TEXT,
        provider_avatar_source TEXT,
        provider_avatar BLOB,
        provider_avatar_digest TEXT,
        PRIMARY KEY (server_id, kind, subject)
    )
"""

IMPORT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS board_assignee_imports (
        ledger_key TEXT PRIMARY KEY,
        subject TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        avatar BLOB,
        avatar_digest TEXT
    )
"""
FIELDS = (
    "server_id, kind, subject, ledger_key, username, name, picture, role, short_role, description, avatar_digest, "
    "provider_description, provider_avatar_source, provider_avatar_digest"
)


def ensure_principals(db, *, versioned=False):
    db.execute(SCHEMA)
    existing = tasks_storage.table_columns(db, "board_principals")
    for field, column_type in (
        ("description", "TEXT"), ("provider_description", "TEXT"), ("provider_avatar_source", "TEXT"),
        ("provider_avatar", "BLOB"), ("provider_avatar_digest", "TEXT"),
    ):
        if field not in existing:
            db.execute(f"ALTER TABLE board_principals ADD COLUMN {field} {column_type}")
    if not versioned and "short_role" not in existing:
        db.execute("ALTER TABLE board_principals ADD COLUMN short_role TEXT")


def retry_provider_photo(db, identity):
    db.execute(
        """
        UPDATE board_principals SET provider_avatar_source = NULL
        WHERE (server_id, kind, subject) = (?, ?, ?) AND avatar_digest IS NULL
            AND provider_avatar_source <> '' AND provider_avatar_digest IS NULL
        """, (identity.server_id, identity.kind, identity.subject),
    )


def for_identity(db, identity):
    return db.execute(
        f"SELECT {FIELDS} FROM board_principals WHERE (server_id, kind, subject) = (?, ?, ?)",
        (identity.server_id, identity.kind, identity.subject),
    ).fetchone()


def signed_in(db, identity, *, picture_source=None, load_picture=None):
    key = (identity.server_id, identity.kind, identity.subject)
    current = for_identity(db, identity)
    old_description = current["provider_description"] if current else None
    try:
        description = description_text(getattr(identity, "description", None) or "")
    except ValueError:
        description = old_description
    # Keep imports as defaults until Raft has supplied a value. An empty string
    # records a later removal so the old import does not return.
    if description == "" and old_description is None:
        description = None
    metadata = (identity.username or "", identity.name or "", description)
    old_source = current["provider_avatar_source"] if current else None
    # A legacy picture stays readable on rollback. An empty source marks that it was checked
    # and the current board has no supported URL to fetch.
    if picture_source is None and current and (old_source is not None or current["picture"]):
        picture_source = ""
    avatar_changed = not (current and current["avatar_digest"]) and picture_source != old_source
    if avatar_changed:
        avatar = load_picture(picture_source) if picture_source else None
    if not current or metadata != (current["username"], current["name"], current["provider_description"]):
        db.execute(
            """
            INSERT INTO board_principals (
                server_id, kind, subject, ledger_key, username, name, provider_description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (server_id, kind, subject) DO UPDATE SET
                username = excluded.username, name = excluded.name,
                provider_description = excluded.provider_description
            """, (*key, f"raft:{identity.kind}:{identity.subject}", *metadata),
        )
    elif not avatar_changed:
        return current
    if avatar_changed:
        save_provider_avatar(db, key, picture_source, avatar)
    # A migration can bind the principal to a pre-existing local handle.
    return for_identity(db, identity)


def save_provider_avatar(db, identity_key, source, avatar):
    return db.execute(
        """
        UPDATE board_principals
        SET provider_avatar_source = ?, provider_avatar = ?, provider_avatar_digest = ?
        WHERE (server_id, kind, subject) = (?, ?, ?) AND avatar_digest IS NULL
        """, (source, avatar, sha256(avatar).hexdigest() if avatar else None, *identity_key),
    ).rowcount


def bind_legacy(db, server_id, kind, subject, old_name):
    server_id, subject = str(UUID(server_id)), str(UUID(subject))
    existing = db.execute(
        "SELECT ledger_key FROM board_principals WHERE (server_id, kind, subject) = (?, ?, ?)",
        (server_id, kind, subject),
    ).fetchone()
    if existing:
        if existing["ledger_key"] == old_name:
            return
        raise ValueError("This principal already has a different ledger key.")
    db.execute(
        "INSERT INTO board_principals (server_id, kind, subject, ledger_key, name) VALUES (?, ?, ?, ?, ?)",
        (server_id, kind, subject, old_name, old_name),
    )


def get(db, key):
    return db.execute(f"SELECT {FIELDS} FROM board_principals WHERE ledger_key = ?", (key,)).fetchone()


def by_keys(db, keys):
    keys = tuple(dict.fromkeys(keys))
    if not keys:
        return {}
    marks = ", ".join("?" for _ in keys)
    rows = db.execute(f"SELECT {FIELDS} FROM board_principals WHERE ledger_key IN ({marks})", keys)
    return {row["ledger_key"]: row for row in rows}


def public_profile(row, base):
    if row is None:
        return None
    digest = row["avatar_digest"] or row["provider_avatar_digest"]
    pending = row["picture"] and row["provider_avatar_source"] is None
    image = digest or ("previous" if pending else None)
    avatar = f"{base}/api/v1/avatars/{row['subject']}/{image}" if image else None
    description = row["description"] if row["description"] is not None else row["provider_description"] or None
    return {
        "id": row["ledger_key"],
        "name": row["name"] or row["username"] or row["ledger_key"],
        "username": row["username"],
        "kind": row["kind"],
        **({"dri_kind": "local" if row["ledger_key"].startswith("local-dri:") else "team"}
           if row["kind"] == "agent" else {}),
        "role": row["role"] or ("Human" if row["kind"] == "human" else None),
        "short_role": row["short_role"],
        "description": description,
        "avatar_url": avatar,
    }


def legacy_avatar(db, subject, resolve, load):
    def lookup():
        return db.execute(
            """
            SELECT picture, provider_avatar_source, provider_avatar, provider_avatar_digest FROM board_principals
            WHERE subject = ? AND avatar_digest IS NULL
            """, (subject,),
        ).fetchone()

    row = lookup()
    if row and row["picture"] and row["provider_avatar_source"] is None:
        previous = row["picture"]
        source = resolve(previous)
        contents = load(source) if source else None
        db.execute(
            """
            UPDATE board_principals
            SET provider_avatar_source = ?, provider_avatar = ?, provider_avatar_digest = ?
            WHERE subject = ? AND picture = ? AND avatar_digest IS NULL AND provider_avatar_source IS NULL
            """, (source or "", contents, sha256(contents).hexdigest() if contents else None, subject, previous),
        )
        row = lookup()
    return row


def update_role(db, key, role):
    role = role.strip()
    if len(role) > 100 or any(not character.isprintable() for character in role):
        raise ValueError("Board roles accept up to 100 printable characters.")
    db.execute("UPDATE board_principals SET role = ? WHERE ledger_key = ?", (role or None, key))


def update_short_role(db, key, short_role):
    if not isinstance(short_role, str):
        raise ValueError("Provide a short role as text.")
    short_role = short_role.strip()
    if short_role and (len(short_role) > MAX_SHORT_ROLE_LENGTH or not short_role.isprintable()):
        raise ValueError(f"Short roles accept up to {MAX_SHORT_ROLE_LENGTH} printable characters on one line.")
    db.execute("UPDATE board_principals SET short_role = ? WHERE ledger_key = ?", (short_role or None, key))


def description_text(description):
    if not isinstance(description, str):
        raise ValueError("Provide a profile description as text.")
    description = description.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Profile descriptions accept up to {MAX_DESCRIPTION_LENGTH} characters.")
    return description


def update_description(db, key, description):
    db.execute("UPDATE board_principals SET description = ? WHERE ledger_key = ?", (description_text(description), key))


def avatar_png(contents):
    if not contents or len(contents) > MAX_AVATAR_BYTES:
        raise ValueError("Choose a PNG, JPEG, or WebP image up to 1 MiB.")
    try:
        with Image.open(BytesIO(contents), formats=["PNG", "JPEG", "WEBP"]) as image:
            if max(image.size) > 4096:
                raise ValueError("Choose an image at most 4096 pixels on either side.")
            avatar = ImageOps.exif_transpose(image).convert("RGBA")
            avatar.thumbnail((AVATAR_SIZE, AVATAR_SIZE))
            rendered = BytesIO()
            avatar.save(rendered, format="PNG")
            png = rendered.getvalue()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("The file is not a readable PNG, JPEG, or WebP image.") from error
    return png


def update_avatar(db, key, contents):
    png = avatar_png(contents)
    db.execute(
        "UPDATE board_principals SET avatar = ?, avatar_digest = ? WHERE ledger_key = ?",
        (png, sha256(png).hexdigest(), key),
    )

def imported_avatar(contents, avatar_url):
    if isinstance(avatar_url, str) and avatar_url.startswith("pixel:") and contents.lstrip().startswith(b"<"):
        if len(contents) > MAX_AVATAR_BYTES:
            raise ValueError("Choose a Raft SVG avatar up to 1 MiB.")
        try:
            if ElementTree.fromstring(contents).tag == SVG_ROOT:
                return contents
        except ElementTree.ParseError:
            pass
        raise ValueError("The Raft pixel avatar must be a readable SVG image.")
    return avatar_png(contents)


def imported_avatar_mimetype(contents):
    return "image/png" if contents.startswith(PNG_SIGNATURE) else "image/svg+xml"


def import_assignee(db, key, record, image):
    """Import read-only profile metadata without creating a login principal."""
    if not isinstance(record, dict) or record.get("ok") is not True or not isinstance(record.get("data"), dict):
        raise ValueError("Provide a successful raft profile show --json response.")
    data = record["data"]
    username = data.get("name")
    kind = data.get("kind")
    if kind not in ("agent", "human") or not isinstance(username, str) or not username or data.get("deletedAt"):
        raise ValueError("Choose an existing Raft profile.")
    try:
        subject = str(UUID(data.get("id", "")))
    except (ValueError, AttributeError) as error:
        raise ValueError("The Raft profile is missing its stable ID.") from error
    if key not in (username, f"raft:{kind}:{subject}"):
        raise ValueError("The ledger owner must match the exact Raft handle or stable key.")
    principal = get(db, key)
    if principal and (principal["kind"], principal["subject"]) != (kind, subject):
        raise ValueError("This owner is bound to a different verified identity.")
    if kind == "human" and principal is None:
        raise ValueError("The human must sign in before importing a profile image.")
    if kind == "agent" and not db.execute("SELECT 1 FROM tasks WHERE dri = ? LIMIT 1", (key,)).fetchone():
        raise ValueError("This owner has no tasks in the selected ledger.")
    imported = db.execute("SELECT subject FROM board_assignee_imports WHERE ledger_key = ?", (key,)).fetchone()
    if imported and imported["subject"] != subject:
        raise ValueError("This owner already has a different imported profile.")
    name = data.get("displayName") or username
    description = data.get("description") or ""
    if (
        not isinstance(name, str) or not isinstance(description, str) or len(name) > 160
        or len(description) > MAX_DESCRIPTION_LENGTH
    ):
        raise ValueError("The Raft display name or description is invalid.")
    if image is not None and not data.get("avatarUrl"):
        raise ValueError("This profile has no Raft avatar to import.")
    avatar = imported_avatar(image, data.get("avatarUrl")) if image is not None else None
    result = db.execute(
        """
        INSERT INTO board_assignee_imports (ledger_key, subject, username, name, description, avatar, avatar_digest)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ledger_key) DO UPDATE SET username = excluded.username, name = excluded.name,
            description = excluded.description, avatar = excluded.avatar, avatar_digest = excluded.avatar_digest
        WHERE board_assignee_imports.username <> excluded.username
            OR board_assignee_imports.name <> excluded.name
            OR board_assignee_imports.description <> excluded.description
            OR board_assignee_imports.avatar_digest IS NOT excluded.avatar_digest
        """, (key, subject, username, name, description, avatar, sha256(avatar).hexdigest() if avatar else None),
    )
    return bool(result.rowcount)


def visible_by_keys(db, keys, base):
    keys = tuple(dict.fromkeys(keys))
    actors = by_keys(db, keys)
    published = {key: public_profile(row, base) for key, row in actors.items()}
    if not keys:
        return published
    marks = ", ".join("?" for _ in keys)
    rows = db.execute(
        f"SELECT ledger_key, subject, username, name, description, avatar_digest FROM board_assignee_imports "
        f"WHERE ledger_key IN ({marks})", keys,
    )
    for row in rows:
        key = row["ledger_key"]
        actor = actors.get(key)
        verified = published.get(key)
        if actor and actor["subject"] != row["subject"]:
            continue
        display = verified or {
            "id": key, "kind": "agent", "username": row["username"],
            "name": row["name"], "role": None, "avatar_url": None,
        }
        if not actor or (actor["description"] is None and actor["provider_description"] is None):
            display["description"] = row["description"]
        # Keep the published URL when Raft supplies the same image bytes.
        if row["avatar_digest"] and (
            not actor or (
                not actor["avatar_digest"]
                and actor["provider_avatar_source"] != ""
                and actor["provider_avatar_digest"] in (None, row["avatar_digest"])
            )
        ):
            display["avatar_url"] = f"{base}/api/v1/assignee-avatar/{row['subject']}/{row['avatar_digest']}"
        published[key] = display
    return published
