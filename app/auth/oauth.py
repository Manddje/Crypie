"""OAuth 2.1 for the MCP endpoint: what Claude's custom connector needs to sign in.

Crypie is both the resource (``/mcp``) and the authorization server, as the MCP authorization
spec allows. The pieces, each the smallest form that works with Claude (claude.ai, Claude
Desktop, Claude Code) and the official MCP clients:

- discovery: ``/.well-known/oauth-protected-resource[/mcp]`` (RFC 9728) points at this host,
  ``/.well-known/oauth-authorization-server`` (RFC 8414) lists the endpoints;
- dynamic client registration (RFC 7591): public clients only (no secret), PKCE with S256
  always, https redirect URIs (or loopback), capped so the open endpoint cannot fill the
  database;
- the authorization code flow with PKCE: the consent page sits behind the normal login and
  asks the current password (a grant is full API access, like the API token);
- the token endpoint: short access tokens, longer refresh tokens that rotate on use;
- revocation: per app on the Account page, or through ``/oauth/revoke``.

Only hashes of codes and tokens are stored. An access token opens exactly what the API token
opens (app/auth/api_token.py): ``/api/*`` and ``/mcp``, never a page, and sensitive changes
still ask the password. The HTTP routes live in app/auth/oauth_routes.py; this module holds
the logic so app/auth/routes.py (the login gate) can use it without a circular import.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import OAuthClient, OAuthCode, OAuthToken

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"

CODE_SECONDS = 5 * 60
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 30 * 24 * 3600
MAX_CLIENTS = 50
MAX_REDIRECT_URIS = 5
UNUSED_CLIENT_DAYS = 1  # a registration that never got a grant is forgotten after this
CLIENT_NAME_MAX = 120
SCOPE_MAX = 256
GRANT_TYPES = ("authorization_code", "refresh_token")
LAST_USED_WRITE_EVERY = timedelta(minutes=1)

ACCESS = "access"
REFRESH = "refresh"


class OAuthError(Exception):
    """An RFC 6749 error: ``error`` (code) and a description for the client."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


class RedirectError(OAuthError):
    """An error the client gets on its redirect URI (the client and URI were valid)."""

    def __init__(self, redirect_uri: str, error: str, description: str):
        super().__init__(error, description)
        self.redirect_uri = redirect_uri


def now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:  # SQLite hands back naive datetimes
        return value.replace(tzinfo=UTC)
    return value


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _token() -> str:
    return secrets.token_urlsafe(32)


# -- metadata ----------------------------------------------------------------------------------


def resource_url(issuer: str) -> str:
    return issuer + MCP_PATH


def resource_metadata(issuer: str) -> dict:
    return {
        "resource": resource_url(issuer),
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_name": "Crypie",
    }


def server_metadata(issuer: str) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + AUTHORIZE_PATH,
        "token_endpoint": issuer + TOKEN_PATH,
        "registration_endpoint": issuer + REGISTER_PATH,
        "revocation_endpoint": issuer + REVOKE_PATH,
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": list(GRANT_TYPES),
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
    }


# -- clients -----------------------------------------------------------------------------------


def valid_redirect_uri(uri: str) -> bool:
    """https with a host, or plain http on the loopback (a local MCP client's callback)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.netloc or "@" in parts.netloc:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def register_client(db: Session, metadata: dict) -> OAuthClient:
    """Dynamic registration (RFC 7591). Raises OAuthError with the RFC's error codes."""
    uris = metadata.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise OAuthError(
            "invalid_redirect_uri", f"redirect_uris: 1 tot {MAX_REDIRECT_URIS} adressen"
        )
    for uri in uris:
        if not isinstance(uri, str) or len(uri) > 2048 or not valid_redirect_uri(uri):
            raise OAuthError("invalid_redirect_uri", f"redirect_uri niet toegestaan: {uri!r}")
    grants = metadata.get("grant_types") or ["authorization_code"]
    if not isinstance(grants, list) or not set(grants) <= set(GRANT_TYPES):
        raise OAuthError("invalid_client_metadata", "grant_types: alleen " + ", ".join(GRANT_TYPES))
    responses = metadata.get("response_types") or ["code"]
    if not isinstance(responses, list) or set(responses) != {"code"}:
        raise OAuthError("invalid_client_metadata", "response_types: alleen code")
    name = metadata.get("client_name") or "Onbekende app"
    if not isinstance(name, str):
        raise OAuthError("invalid_client_metadata", "client_name moet tekst zijn")
    name = " ".join(name.split())[:CLIENT_NAME_MAX] or "Onbekende app"
    _prune_clients(db)
    if db.scalar(select(OAuthClient).limit(1).offset(MAX_CLIENTS - 1)) is not None:
        raise OAuthError(
            "invalid_client_metadata",
            f"er zijn al {MAX_CLIENTS} apps geregistreerd; trek er een in op de accountpagina",
        )
    client = OAuthClient(
        client_id=secrets.token_urlsafe(24), name=name, redirect_uris=json.dumps(uris)
    )
    db.add(client)
    db.commit()
    log.info("OAuth client registered: %s (%s)", name, client.client_id)
    return client


def _prune_clients(db: Session) -> None:
    """Forget registrations that never got a grant and are older than a day."""
    cutoff = now() - timedelta(days=UNUSED_CLIENT_DAYS)
    with_tokens = set(db.scalars(select(OAuthToken.client_id).distinct()).all())
    for client in db.scalars(select(OAuthClient)).all():
        created = _aware(client.created_at) or now()
        if client.client_id not in with_tokens and created < cutoff:
            db.delete(client)
    db.commit()


def client_registration(client: OAuthClient) -> dict:
    created = _aware(client.created_at) or now()
    return {
        "client_id": client.client_id,
        "client_id_issued_at": int(created.timestamp()),
        "client_name": client.name,
        "redirect_uris": json.loads(client.redirect_uris),
        "grant_types": list(GRANT_TYPES),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def get_client(db: Session, client_id: str | None) -> OAuthClient | None:
    if not client_id or len(client_id) > 64:
        return None
    return db.get(OAuthClient, client_id)


def redirect_allowed(client: OAuthClient, redirect_uri: str | None) -> bool:
    return bool(redirect_uri) and redirect_uri in json.loads(client.redirect_uris)


# -- authorization codes ---------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizeRequest:
    """The checked parameters of ``GET /oauth/authorize``."""

    client: OAuthClient
    redirect_uri: str
    code_challenge: str
    state: str | None
    scope: str
    resource: str | None


def check_authorize(db: Session, params: dict, issuer: str) -> AuthorizeRequest:
    """Validate an authorization request. Errors about the client or the redirect URI are a
    plain OAuthError (shown, never redirected); other errors are a RedirectError, which the
    client gets on its redirect URI (RFC 6749 4.1.2.1)."""
    client = get_client(db, params.get("client_id"))
    if client is None:
        raise OAuthError("invalid_client", "Onbekende app (client_id).")
    redirect_uri = params.get("redirect_uri")
    if not redirect_allowed(client, redirect_uri):
        raise OAuthError(
            "invalid_request", "Het antwoordadres (redirect_uri) hoort niet bij deze app."
        )
    assert redirect_uri is not None
    if params.get("response_type") != "code":
        raise RedirectError(redirect_uri, "unsupported_response_type", "alleen response_type=code")
    challenge = params.get("code_challenge") or ""
    if params.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
        raise RedirectError(redirect_uri, "invalid_request", "PKCE met S256 is verplicht")
    scope = " ".join((params.get("scope") or "").split())[:SCOPE_MAX]
    resource = params.get("resource") or None
    if resource is not None and resource.rstrip("/") != resource_url(issuer):
        raise RedirectError(redirect_uri, "invalid_target", "resource moet " + resource_url(issuer))
    state = params.get("state") or None
    if state is not None and len(state) > 1024:
        raise RedirectError(redirect_uri, "invalid_request", "state te lang")
    return AuthorizeRequest(client, redirect_uri, challenge, state, scope, resource)


def issue_code(db: Session, req: AuthorizeRequest) -> str:
    code = _token()
    db.add(
        OAuthCode(
            code_hash=_hash(code),
            client_id=req.client.client_id,
            redirect_uri=req.redirect_uri,
            code_challenge=req.code_challenge,
            scope=req.scope,
            resource=req.resource,
            expires_at=now() + timedelta(seconds=CODE_SECONDS),
        )
    )
    db.commit()
    return code


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):
        return False
    digest = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(digest, challenge)


# -- tokens ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str

    def body(self) -> dict:
        body = {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
        }
        if self.scope:
            body["scope"] = self.scope
        return body


def _issue_grant(db: Session, client: OAuthClient, scope: str, family: str) -> Grant:
    access, refresh = _token(), _token()
    at = now()
    db.add(
        OAuthToken(
            token_hash=_hash(access),
            kind=ACCESS,
            client_id=client.client_id,
            family=family,
            scope=scope,
            expires_at=at + timedelta(seconds=ACCESS_SECONDS),
        )
    )
    db.add(
        OAuthToken(
            token_hash=_hash(refresh),
            kind=REFRESH,
            client_id=client.client_id,
            family=family,
            scope=scope,
            expires_at=at + timedelta(seconds=REFRESH_SECONDS),
        )
    )
    client.last_used_at = at
    db.commit()
    return Grant(access, refresh, ACCESS_SECONDS, scope)


def exchange_code(db: Session, form: dict, issuer: str) -> Grant:
    """``grant_type=authorization_code``: one-time code plus PKCE verifier -> tokens."""
    client = get_client(db, form.get("client_id"))
    if client is None:
        raise OAuthError("invalid_client", "Onbekende app (client_id).", 401)
    code = form.get("code") or ""
    row = db.get(OAuthCode, _hash(code)) if code and len(code) <= 256 else None
    if row is None or row.client_id != client.client_id:
        raise OAuthError("invalid_grant", "De code is onbekend, al gebruikt of verlopen.")
    expires = _aware(row.expires_at) or now()
    redirect_uri, challenge, scope = row.redirect_uri, row.code_challenge, row.scope
    db.delete(row)  # one-time, whatever happens next
    db.commit()
    if expires < now():
        raise OAuthError("invalid_grant", "De code is verlopen.")
    if form.get("redirect_uri") != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri wijkt af van de aanvraag.")
    if not _pkce_ok(form.get("code_verifier") or "", challenge):
        raise OAuthError("invalid_grant", "De PKCE-verifier klopt niet.")
    resource = form.get("resource")
    if resource and resource.rstrip("/") != resource_url(issuer):
        raise OAuthError("invalid_target", "resource moet " + resource_url(issuer))
    grant = _issue_grant(db, client, scope, family=secrets.token_urlsafe(16))
    log.warning("OAuth access granted to %s (%s)", client.name, client.client_id)
    return grant


def refresh_grant(db: Session, form: dict) -> Grant:
    """``grant_type=refresh_token``: rotate; the old refresh token and its access tokens die."""
    client = get_client(db, form.get("client_id"))
    if client is None:
        raise OAuthError("invalid_client", "Onbekende app (client_id).", 401)
    token = form.get("refresh_token") or ""
    row = db.get(OAuthToken, _hash(token)) if token and len(token) <= 256 else None
    if row is None or row.kind != REFRESH or row.client_id != client.client_id:
        raise OAuthError("invalid_grant", "Het refresh-token is onbekend of ingetrokken.")
    family, scope = row.family, row.scope
    expired = (_aware(row.expires_at) or now()) < now()
    db.execute(delete(OAuthToken).where(OAuthToken.family == family))
    db.commit()
    if expired:
        raise OAuthError("invalid_grant", "Het refresh-token is verlopen; koppel opnieuw.")
    return _issue_grant(db, client, scope, family)


def access_token_valid(db: Session, token: str | None) -> OAuthClient | None:
    """The app behind a live access token, or None. Notes when it was last used."""
    if not token or len(token) > 256:
        return None
    row = db.get(OAuthToken, _hash(token))
    if row is None or row.kind != ACCESS:
        return None
    at = now()
    if (_aware(row.expires_at) or at) < at:
        return None
    last = _aware(row.last_used_at)
    if last is None or at - last > LAST_USED_WRITE_EVERY:
        row.last_used_at = at
        db.commit()
    return db.get(OAuthClient, row.client_id)


def revoke_token(db: Session, token: str | None) -> bool:
    """RFC 7009: forget the token and everything from the same grant. True when it existed."""
    if not token or len(token) > 256:
        return False
    row = db.get(OAuthToken, _hash(token))
    if row is None:
        return False
    db.execute(delete(OAuthToken).where(OAuthToken.family == row.family))
    db.commit()
    return True


def revoke_client(db: Session, client_id: str | None) -> OAuthClient | None:
    """Forget an app: its tokens, its codes and its registration (the Account page)."""
    client = get_client(db, client_id)
    if client is None:
        return None
    db.execute(delete(OAuthToken).where(OAuthToken.client_id == client.client_id))
    db.execute(delete(OAuthCode).where(OAuthCode.client_id == client.client_id))
    db.delete(client)
    db.commit()
    return client


def prune_expired(db: Session) -> None:
    at = now()
    db.execute(delete(OAuthCode).where(OAuthCode.expires_at < at))
    db.execute(delete(OAuthToken).where(OAuthToken.expires_at < at))
    db.commit()


@dataclass(frozen=True)
class ConnectedApp:
    client_id: str
    name: str
    since: datetime | None
    last_used: datetime | None
    valid_until: datetime | None  # the refresh token's end: when the app has to be re-linked


def connected_apps(db: Session) -> list[ConnectedApp]:
    """Apps that hold a live refresh token (what the Account page lists)."""
    prune_expired(db)
    apps = []
    for client in db.scalars(select(OAuthClient).order_by(OAuthClient.created_at)).all():
        tokens = db.scalars(
            select(OAuthToken).where(OAuthToken.client_id == client.client_id)
        ).all()
        if not any(t.kind == REFRESH for t in tokens):
            continue
        used = [_aware(t.last_used_at) for t in tokens if t.last_used_at is not None]
        apps.append(
            ConnectedApp(
                client.client_id,
                client.name,
                _aware(client.created_at),
                max(used, default=None),
                max((_aware(t.expires_at) for t in tokens if t.kind == REFRESH), default=None),
            )
        )
    return apps
