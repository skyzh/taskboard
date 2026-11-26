"""Verified browser identity and Taskboard sessions for member-based sign-in."""

from dataclasses import dataclass
from json import dumps
from math import ceil, isfinite
from pathlib import Path
from secrets import token_urlsafe
from time import time
from typing import Callable, Protocol
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, make_response, redirect, request, session

from raft_auth import login_return
from raft_sessions import DurableSessions, Session
from tasks_oidc_members import issuer_url, valid_subject

LOGIN_SECONDS = 300
SESSION_SECONDS = 3600


@dataclass(frozen=True)
class VerifiedLogin:
    """An identity verified by the selected provider for this client and sign-in attempt."""

    issuer: str
    subject: str
    expires_at: float
    name: str | None = None
    picture: str | None = None


class LoginRejected(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class LoginAdapter(Protocol):
    def begin(self, callback_url: str) -> Response: ...

    def verify(self) -> VerifiedLogin: ...


@dataclass(frozen=True)
class LoginProvider:
    """Trusted deployment configuration for one member-based browser provider."""

    issuer: str
    client_id: str
    session_dir: str | Path
    adapter: Callable[[Flask], LoginAdapter]


def external_issuer(issuer):
    # The same issuer URL in OIDC and a deployment adapter represents separate bindings.
    return "external:" + issuer_url(issuer)


class BrowserLogin:
    def __init__(self, app, public_url, *, provider, mode, admit, resolve):
        self.issuer = issuer_url(provider.issuer)
        if not provider.client_id or not provider.session_dir:
            raise ValueError("Configure the login provider client and private session directory.")
        try:
            self.public_url = issuer_url(public_url)
        except ValueError as error:
            raise ValueError("Set an HTTPS board origin and mount path for sign-in.") from error
        self.admit = admit
        self.resolve = resolve
        host = urlsplit(public_url)
        self.origin = f"{host.scheme}://{host.netloc}"
        self.cookie_path = host.path + "/"
        self.cookie = f"tasks_{mode}_session"
        self.sessions = DurableSessions(
            provider.session_dir, dumps([self.issuer, provider.client_id, public_url]),
            filename=f"{mode}-sessions.sqlite3",
        )
        app.secret_key = token_urlsafe(32)
        app.config.update(
            SESSION_COOKIE_NAME=f"tasks_{mode}_login", SESSION_COOKIE_PATH=host.path + "/auth/",
            SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
            PERMANENT_SESSION_LIFETIME=LOGIN_SECONDS, SESSION_REFRESH_EACH_REQUEST=False,
        )
        self.adapter = provider.adapter(app)
        app.add_url_rule("/auth/login", view_func=self.login)
        app.add_url_rule("/auth/callback", view_func=self.callback)
        app.add_url_rule("/auth/logout", view_func=self.logout, methods=["POST"])

    def login(self):
        return_target = login_return(request.args)
        if return_target is None:
            return jsonify(error="Open the sign-in link from Taskboard."), 400
        session.clear()
        session["expires"] = time() + LOGIN_SECONDS
        session["return_to"] = return_target
        try:
            return self.adapter.begin(self.public_url + "/auth/callback")
        except LoginRejected as rejected:
            session.clear()
            return jsonify(error=str(rejected)), rejected.status

    def callback(self):
        return_target = session.pop("return_to", "/" + session.pop("fragment", ""))
        expires = session.pop("expires", 0)
        try:
            if expires <= time():
                raise LoginRejected("Start a new sign-in.", 400)
            identity = self.adapter.verify()
            now = time()
            if (identity.issuer != self.issuer or not valid_subject(identity.subject)
                    or type(identity.expires_at) not in (int, float)
                    or not isfinite(identity.expires_at) or identity.expires_at <= now):
                raise LoginRejected("Sign-in could not be verified.", 401)
            if not self.admit(identity, self.adapter):
                raise LoginRejected("This account has no Taskboard membership.", 403)
            response = redirect(self.public_url + return_target)
            key = token_urlsafe(32)
            end = min(identity.expires_at, now + SESSION_SECONDS)
            self.sessions.replace(request.cookies.get(self.cookie), key, Session("", end, identity.subject, "human"), now)
            response.set_cookie(self.cookie, key, max_age=ceil(end - now), path=self.cookie_path,
                                secure=True, httponly=True, samesite="Lax")
            return response
        except LoginRejected as rejected:
            return jsonify(error=str(rejected)), rejected.status
        finally:
            session.clear()

    def actor(self):
        key = request.cookies.get(self.cookie)
        saved = self.sessions.get(key, time()) if key else None
        return self.resolve(saved.subject) if saved else None

    def logout(self):
        if request.headers.get("Origin") not in (None, self.origin):
            return jsonify(error="Send changes from the board origin."), 403
        self.sessions.discard(request.cookies.get(self.cookie))
        session.clear()
        response = make_response("", 204)
        response.delete_cookie(self.cookie, path=self.cookie_path, secure=True, httponly=True, samesite="Lax")
        return response
