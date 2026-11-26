"""OpenID Connect sign-in for a self-hosted Taskboard."""

from hashlib import sha256
from time import time
from urllib.parse import urlsplit

from flask import request

from raft_auth import fetch_profile_photo
from taskboard_server.login import LoginProvider, LoginRejected, VerifiedLogin
from tasks_oidc_members import issuer_url, valid_subject


SIGNED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
REQUIRED = ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "SESSION_DIR")


def configuration(environment):
    mode = environment.get("TASKS_IDENTITY_PROVIDER", "raft")
    if mode not in ("raft", "oidc"):
        raise ValueError("Set TASKS_IDENTITY_PROVIDER to raft or oidc.")
    oidc_settings = {name.lower(): environment.get(f"TASKS_OIDC_{name}") for name in REQUIRED}
    photo_origin = environment.get("TASKS_OIDC_PHOTO_ORIGIN")
    if mode == "raft":
        if any(oidc_settings.values()) or photo_origin:
            raise ValueError("Select the oidc identity provider before setting OpenID Connect credentials.")
        return None
    if not all(oidc_settings.values()):
        raise ValueError("Set the OpenID Connect issuer, client ID, client secret and private session directory.")
    if any(environment.get(name) for name in [
        "TASKS_RAFT_API_ORIGIN", "TASKS_RAFT_SERVER_ID", "TASKS_RAFT_CLIENT_ID", "TASKS_RAFT_CLIENT_SECRET",
        "TASKS_RAFT_SESSION_DIR", "TASKS_RAFT_HUMAN_SESSION_INSTALLATION_ID", "TASKS_MARKETPLACE_SESSION_DIR",
    ]):
        raise ValueError("Start OpenID Connect in an environment without Raft provider configuration.")
    return {**oidc_settings, "photo_origin": photo_origin or None}


def visible_url(value):
    return isinstance(value, str) and all(32 < ord(character) < 127 and character != "\\" for character in value)


def configured_photo_origin(value):
    if value is None:
        return None
    try:
        if visible_url(value) and urlsplit(issuer_url(value)).path in ("", "/"):
            return value.rstrip("/")
    except ValueError:
        pass
    raise ValueError("Set TASKS_OIDC_PHOTO_ORIGIN to one HTTPS origin without a path.")


def allowed_endpoint(value):
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and not parsed.fragment
    except ValueError:
        return False


def provider(*, issuer, client_id, client_secret, session_dir, photo_origin=None):
    def adapter(app):
        return OIDC(app, issuer=issuer, client_id=client_id, client_secret=client_secret, photo_origin=photo_origin)

    return LoginProvider(issuer, client_id, session_dir, adapter)


class OIDC:
    def __init__(self, app, *, issuer, client_id, client_secret, photo_origin):
        try:
            from authlib.integrations.base_client import OAuthError
            from authlib.integrations.flask_client import OAuth
            from authlib.oidc.core import UserInfo
            from joserfc.errors import JoseError
            from requests import RequestException
        except ModuleNotFoundError as failure:
            raise ValueError("Install OpenID Connect with uv sync --frozen --extra oidc.") from failure
        self.issuer = issuer_url(issuer)
        self.photo_origin = configured_photo_origin(photo_origin)
        if not client_secret:
            raise ValueError("Configure the OpenID Connect client secret.")
        self.client_id = client_id
        self.validated_claims = UserInfo
        self.failures = (OAuthError, JoseError, RequestException, ValueError)
        self.client = OAuth(app).register(
            "taskboard_oidc", client_id=client_id, client_secret=client_secret,
            server_metadata_url=self.issuer.rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid profile", "code_challenge_method": "S256", "default_timeout": 8},
        )

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

    def begin(self, callback_url):
        try:
            self.metadata()
            return self.client.authorize_redirect(callback_url)
        except self.failures:
            raise LoginRejected("OpenID Connect sign-in is unavailable.", 503) from None

    def verify(self):
        states, codes, issuer = (request.args.getlist(key) for key in ("state", "code", "iss"))
        if len(states) != 1 or len(codes) != 1 or not codes[0] or (issuer and issuer != [self.issuer]):
            raise LoginRejected("Start a new OpenID Connect sign-in.", 400)
        try:
            self.metadata()
            token = self.client.authorize_access_token(leeway=0)
            claims = token.get("userinfo")
            if not isinstance(claims, self.validated_claims) or not token.get("id_token"):
                raise ValueError("Unverified identity")
            aud, subject, expiry = (claims.get(key) for key in ("aud", "sub", "exp"))
            if (claims.get("iss") != self.issuer or aud not in (self.client_id, [self.client_id])
                    or not valid_subject(subject) or type(expiry) is not int or expiry <= time()):
                raise ValueError("Unverified identity")
            return VerifiedLogin(self.issuer, subject, expiry, claims.get("name"), claims.get("picture"))
        except self.failures:
            raise LoginRejected("OpenID Connect sign-in could not be verified.", 401) from None

    def profile_photo(self, actor, picture):
        if (not self.photo_origin or actor["avatar_digest"] or not visible_url(picture)
                or not picture.startswith(self.photo_origin + "/") or "#" in picture):
            return None
        # A signed image URL can carry private query parameters. Store only its digest.
        source = sha256(picture.encode()).hexdigest()
        if actor["provider_avatar_source"] == source and actor["provider_avatar_digest"]:
            return None
        contents = fetch_profile_photo(picture)
        return (source, contents) if contents else None
