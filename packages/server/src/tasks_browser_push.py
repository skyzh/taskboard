"""Browser subscriptions and best-effort delivery for committed human asks."""

from base64 import b64decode, urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from time import time
from urllib.parse import urlsplit
from uuid import uuid4

import tasks_core as ledger
import tasks_storage


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS browser_push_subscriptions (
        endpoint TEXT PRIMARY KEY, recipient TEXT NOT NULL, session TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL, auth TEXT NOT NULL, expires_at BIGINT, enrollment TEXT NOT NULL UNIQUE
    )""",
    """CREATE INDEX IF NOT EXISTS browser_push_recipient ON browser_push_subscriptions (recipient)""",
    """CREATE TABLE IF NOT EXISTS browser_push_receipts (
        endpoint TEXT NOT NULL REFERENCES browser_push_subscriptions(endpoint) ON DELETE CASCADE,
        kind TEXT NOT NULL, ask_id BIGINT NOT NULL, PRIMARY KEY (endpoint, kind, ask_id)
    )""",
)
PUSH_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"})


def decode_key(value, length):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Invalid Web Push key.")
    try:
        key = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise ValueError("Invalid Web Push key.") from None
    if len(key) != length:
        raise ValueError("Invalid Web Push key.")
    return key


def subscription(data):
    from cryptography.hazmat.primitives.asymmetric import ec

    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or not endpoint.isascii():
        raise ValueError("Use a supported browser push endpoint.")
    host = urlsplit(endpoint)
    if (host.scheme != "https" or host.netloc not in PUSH_HOSTS or host.fragment or not host.path.strip("/")
            or any(character.isspace() for character in endpoint)):
        raise ValueError("Use a supported browser push endpoint.")
    keys = data.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValueError("Include the browser push keys.")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode_key(keys["p256dh"], 65))
    except ValueError:
        raise ValueError("Invalid Web Push public key.") from None
    decode_key(keys["auth"], 16)
    expiry = data.get("expirationTime")
    if expiry is not None and (type(expiry) is not int or not time() * 1000 < expiry <= 2**53 - 1):
        raise ValueError("The browser push subscription has expired.")
    return endpoint, keys["p256dh"], keys["auth"], expiry


def save(db, owner, session, data):
    endpoint, p256dh, auth, expiry = subscription(data)
    db.execute("DELETE FROM browser_push_subscriptions WHERE session = ? AND endpoint <> ?", (session, endpoint))
    db.execute("""
        INSERT INTO browser_push_subscriptions (endpoint, recipient, session, p256dh, auth, expires_at, enrollment)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (endpoint) DO UPDATE SET recipient = excluded.recipient, session = excluded.session,
            p256dh = excluded.p256dh, auth = excluded.auth, expires_at = excluded.expires_at,
            enrollment = excluded.enrollment
    """, (endpoint, owner, session, p256dh, auth, expiry, uuid4().hex))


def remove(db, session):
    db.execute("DELETE FROM browser_push_subscriptions WHERE session = ?", (session,))


def enrollment(db, session):
    row = db.execute("""
        SELECT enrollment FROM browser_push_subscriptions WHERE session = ?
        AND (expires_at IS NULL OR expires_at > ?)
    """, (session, int(time() * 1000))).fetchone()
    return row["enrollment"] if row else None


def remove_enrollment(db, value):
    db.execute("DELETE FROM browser_push_subscriptions WHERE enrollment = ?", (value,))


def recipient_for(db, kind, ticket, ask_id):
    if kind == "decision":
        task = ledger.find_task(db, ticket)
        if task and task["state"] == "human_review" and task["event_id"] == ask_id:
            return task["waiting_on"]
        return None
    row = db.execute("""
        SELECT recipient FROM attention_requests WHERE id = ? AND task_id = ?
        AND handled_at IS NULL AND remind_on IS NULL
    """, (ask_id, ticket)).fetchone()
    return row["recipient"] if row else None


@dataclass(frozen=True)
class Configuration:
    public_key: str
    send: object


def configuration(environ):
    key_file = environ.get("TASKS_WEB_PUSH_VAPID_KEY_FILE")
    if not key_file:
        return None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    from requests import RequestException, Session

    contact = environ.get("TASKS_WEB_PUSH_CONTACT", "")
    if not contact.startswith("mailto:") or "@" not in contact:
        raise ValueError("Set TASKS_WEB_PUSH_CONTACT to a mailto URI.")
    material = Path(key_file).read_bytes()
    key = serialization.load_pem_private_key(material, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Use a P-256 Web Push VAPID private key.")
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    signing = Vapid.from_pem(material)

    class PushTransport(Session):
        def post(self, url, *args, **kwargs):
            return super().post(url, *args, allow_redirects=False, **kwargs)

    def send(row, ttl):
        try:
            with PushTransport() as transport:
                response = webpush(
                    {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
                    data=json.dumps({"enrollment": row["enrollment"]}),
                    vapid_private_key=signing, vapid_claims={"sub": contact}, ttl=ttl,
                    content_encoding="aes128gcm", timeout=3, requests_session=transport,
                )
            return response.status_code
        except WebPushException as error:
            return error.response.status_code if error.response is not None else None
        except RequestException:
            return None

    return Configuration(urlsafe_b64encode(public).decode().rstrip("="), send)


def operator_configuration(environ):
    try:
        return configuration(environ)
    except (ImportError, OSError, TypeError, ValueError):
        logging.getLogger(__name__).warning("Browser push is disabled. Check the Web Push extra and VAPID configuration.")
        return None


class Delivery:
    def __init__(self, app, database, auth, config):
        self.app = app
        self.database = database
        self.auth = auth
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="taskboard-browser-push")

    def submit(self, kind, ticket, ask_id):
        try:
            self.executor.submit(self.deliver, kind, ticket, ask_id).add_done_callback(self.report_failure)
        except RuntimeError:
            self.app.logger.warning("Browser push delivery is unavailable.")

    def report_failure(self, result):
        if result.exception():
            self.app.logger.warning("Browser push delivery failed.")

    def deliver(self, kind, ticket, ask_id):
        with self.app.app_context():
            db = self.database()
            recipient = recipient_for(db, kind, ticket, ask_id)
            if not recipient:
                return
            rows = db.execute("""
                SELECT * FROM browser_push_subscriptions WHERE recipient = ?
                AND (expires_at IS NULL OR expires_at > ?)
            """, (recipient, int(time() * 1000))).fetchall()
            for row in rows:
                self.deliver_subscription(db, row, kind, ticket, ask_id)

    def deliver_subscription(self, db, row, kind, ticket, ask_id):
        remaining = self.auth.human_session_remaining(row["session"])
        if remaining is None or remaining < 1:
            with tasks_storage.write(db):
                remove_enrollment(db, row["enrollment"])
            return
        expires = row["expires_at"]
        ttl = min(300, int(remaining))
        if expires is not None:
            ttl = min(ttl, int((expires - time() * 1000) / 1000))
        if ttl < 1:
            return
        with tasks_storage.write(db):
            current = db.execute(
                "SELECT enrollment FROM browser_push_subscriptions WHERE endpoint = ?", (row["endpoint"],),
            ).fetchone()
            if not current or current["enrollment"] != row["enrollment"]:
                return
            if recipient_for(db, kind, ticket, ask_id) != row["recipient"]:
                return
            claimed = db.execute("""
                INSERT INTO browser_push_receipts (endpoint, kind, ask_id) VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
            """, (row["endpoint"], kind, ask_id)).rowcount
        if not claimed:
            return
        try:
            status = self.config.send(row, ttl)
        except Exception:
            self.app.logger.warning("Could not prepare a push for one browser.")
            return
        if status in (404, 410):
            with tasks_storage.write(db):
                remove_enrollment(db, row["enrollment"])
