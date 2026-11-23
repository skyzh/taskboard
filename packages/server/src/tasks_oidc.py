"""OpenID Connect sign-in for a self-hosted Taskboard."""

from json import dumps
from math import ceil
from secrets import token_urlsafe
from time import time
from urllib.parse import urlsplit

from flask import jsonify, make_response, redirect, request, session

from raft_auth import BOARD_FRAGMENT
from raft_sessions import DurableSessions, Session
from tasks_oidc_members import issuer_url, valid_subject


COOKIE = "tasks_oidc_session"
LOGIN_SECONDS = 300
SESSION_SECONDS = 3600
SIGNED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
REQUIRED = ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "SESSION_DIR")


def configuration(environment):
    mode = environment.get("TASKS_IDENTITY_PROVIDER", "raft")
    if mode not in ("raft", "oidc"):
        raise ValueError("Set TASKS_IDENTITY_PROVIDER to raft or oidc.")
    oidc_settings = {name.lower(): environment.get(f"TASKS_OIDC_{name}") for name in REQUIRED}
    if mode == "raft":
        if any(oidc_settings.values()):
            raise ValueError("Select the oidc identity provider before setting OpenID Connect credentials.")
        return None
    if not all(oidc_settings.values()):
        raise ValueError("Set the OpenID Connect issuer, client ID, client secret and private session directory.")
    if any(environment.get(name) for name in [
        "TASKS_RAFT_API_ORIGIN", "TASKS_RAFT_SERVER_ID", "TASKS_RAFT_CLIENT_ID", "TASKS_RAFT_CLIENT_SECRET",
        "TASKS_RAFT_SESSION_DIR", "TASKS_RAFT_HUMAN_SESSION_INSTALLATION_ID", "TASKS_MARKETPLACE_SESSION_DIR",
    ]):
        raise ValueError("Start OpenID Connect in an environment without Raft provider configuration.")
    return oidc_settings


def error(message, status):
    return jsonify(error=message), status


def allowed_endpoint(value):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and not parsed.fragment
    except ValueError:
        return False


class OIDC:
    def __init__(self, app, public_url, *, issuer, client_id, client_secret, session_dir, admit, resolve):
        try:
            from authlib.integrations.base_client import OAuthError
            from authlib.integrations.flask_client import OAuth
            from authlib.oidc.core import UserInfo
            from joserfc.errors import JoseError
            from requests import RequestException
        except ModuleNotFoundError as failure:
            raise ValueError("Install OpenID Connect with uv sync --frozen --extra oidc.") from failure
        self.issuer = issuer_url(issuer)
        if not (client_id and client_secret and session_dir):
            raise ValueError("Configure the OpenID Connect client and private session directory.")
        self.public_url = public_url
        self.client_id = client_id
        self.validated_claims = UserInfo
        parsed = urlsplit(public_url)
        if not allowed_endpoint(public_url) or "?" in public_url or "#" in public_url:
            raise ValueError("Set an HTTPS Taskboard origin and mount path for OpenID Connect.")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.cookie_path = parsed.path + "/"
        self.admit = admit
        self.resolve = resolve
        self.failures = (OAuthError, JoseError, RequestException, ValueError)
        self.sessions = DurableSessions(
            session_dir, dumps([self.issuer, client_id, public_url]), filename="oidc-sessions.sqlite3",
        )

        app.secret_key = token_urlsafe(32)
        app.config.update(
            SESSION_COOKIE_NAME="tasks_oidc_login", SESSION_COOKIE_PATH=parsed.path + "/auth/",
            SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
            PERMANENT_SESSION_LIFETIME=LOGIN_SECONDS, SESSION_REFRESH_EACH_REQUEST=False,
        )
        self.client = OAuth(app).register(
            "taskboard_oidc", client_id=client_id, client_secret=client_secret,
            server_metadata_url=self.issuer.rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile", "code_challenge_method": "S256", "default_timeout": 8},
        )
        app.add_url_rule("/auth/login", view_func=self.login)
        app.add_url_rule("/auth/callback", view_func=self.callback)
        app.add_url_rule("/auth/logout", view_func=self.logout, methods=["POST"])

    def metadata(self):
        metadata = self.client.load_server_metadata()
        if metadata.get("issuer") != self.issuer or not all(
            allowed_endpoint(metadata.get(key)) for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")
        ):
            raise ValueError("OpenID Connect discovery did not match the configured issuer.")
        algorithms = metadata.get("id_token_signing_alg_values_supported", ["RS256"])
        if not isinstance(algorithms, list):
            raise ValueError("OpenID Connect discovery did not advertise a valid signing algorithm.")
        metadata["id_token_signing_alg_values_supported"] = [name for name in SIGNED_ALGORITHMS if name in algorithms]
        if not metadata["id_token_signing_alg_values_supported"]:
            raise ValueError("OpenID Connect requires a signed ID token.")
        return metadata

    def login(self):
        fragments = request.args.getlist("fragment")
        if fragments and (len(fragments) != 1 or not BOARD_FRAGMENT.fullmatch(fragments[0])):
            return error("Open the sign-in link from Taskboard.", 400)
        session.clear()
        try:
            self.metadata()
            session["expires"] = time() + LOGIN_SECONDS
            session["fragment"] = fragments[0] if fragments else ""
            return self.client.authorize_redirect(self.public_url + "/auth/callback")
        except self.failures:
            session.clear()
            return error("OpenID Connect sign-in is unavailable.", 503)

    def callback(self):
        states, codes, issuer = (request.args.getlist(key) for key in ("state", "code", "iss"))
        fragment = session.pop("fragment", "")
        expires = session.pop("expires", 0)
        if (expires <= time() or len(states) != 1 or len(codes) != 1 or not codes[0]
                or (issuer and issuer != [self.issuer])):
            session.clear()
            return error("Start a new OpenID Connect sign-in.", 400)
        try:
            self.metadata()
            token = self.client.authorize_access_token(leeway=0)
            claims = token.get("userinfo")
            if not isinstance(claims, self.validated_claims) or not token.get("id_token"):
                return error("OpenID Connect sign-in could not be verified.", 401)
            aud, subject, expiry = (claims.get(key) for key in ("aud", "sub", "exp"))
            now = time()
            if (
                claims.get("iss") != self.issuer or aud not in (self.client_id, [self.client_id])
                or not valid_subject(subject) or type(expiry) is not int or expiry <= now
            ):
                return error("OpenID Connect sign-in could not be verified.", 401)
            if not self.admit(subject, claims.get("name")):
                return error("This account has no Taskboard membership.", 403)
            response = redirect(f"{self.public_url}/{fragment}")
            key = token_urlsafe(32)
            end = min(expiry, now + SESSION_SECONDS)
            self.sessions.replace(request.cookies.get(COOKIE), key, Session("", end, subject, "human"), now)
            response.set_cookie(COOKIE, key, max_age=ceil(end - now), path=self.cookie_path,
                                secure=True, httponly=True, samesite="Lax")
            return response
        except self.failures:
            return error("OpenID Connect sign-in could not be verified.", 401)
        finally:
            session.clear()

    def actor(self):
        key = request.cookies.get(COOKIE)
        saved = self.sessions.get(key, time()) if key else None
        return self.resolve(saved.subject) if saved else None

    def logout(self):
        if request.headers.get("Origin") not in (None, self.origin):
            return error("Send changes from the board origin.", 403)
        self.sessions.discard(request.cookies.get(COOKIE))
        session.clear()
        response = make_response("", 204)
        response.delete_cookie(COOKIE, path=self.cookie_path, secure=True, httponly=True, samesite="Lax")
        return response
