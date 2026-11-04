"""A bearer token for the JSON API and the MCP endpoint, next to the browser login.

The token lets a program call ``/api/*`` and ``/mcp`` with ``Authorization: Bearer <token>``:
that is how Claude's MCP connection (app/mcp_server.py) gets in, without a password, an
authenticator code or a cookie. It only counts on those paths; a page keeps asking for the
login, so a token in a browser opens nothing. Sensitive changes still ask the current password
(step-up), also with the token.

Two sources, the portal first: a token made on the Account page (``issue_token``) is kept as
its SHA-256 in the settings table (a database backup does not give it away) and is shown once;
while one exists only that token counts. Without one, ``API_TOKEN`` from the environment (or
``/run/secrets/api_token``) applies; with fewer than ``API_TOKEN_MIN_LENGTH`` characters it is
ignored (app/config.py warns), so a weak token never opens anything.

Comparisons are constant time over the SHA-256 of both sides (a wrong token of another length
takes as long as one of the right length). The login middleware that uses this lives in
app/auth/routes.py (``require_login_or_token``).
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.config import API_TOKEN_MIN_LENGTH
from app.db import Setting

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = API_TOKEN_MIN_LENGTH
TOKEN_HASH_KEY = "auth:api_token_hash"
TOKEN_BYTES = 32  # token_urlsafe(32): 43 characters
MCP_PATH = "/mcp"
API_PREFIX = "/api/"
BEARER = "bearer "

PORTAL = "portal"
ENVIRONMENT = "env"


def token_path(path: str) -> bool:
    """Paths on which the token counts: the JSON API and the MCP endpoint."""
    return path == MCP_PATH or path.startswith(API_PREFIX)


def bearer_token(request: Request) -> str | None:
    """The token of an ``Authorization: Bearer ...`` header, or None without one."""
    header = request.headers.get("authorization", "")
    if header[: len(BEARER)].lower() != BEARER:
        return None
    return header[len(BEARER) :].strip()


def _hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def token_matches(provided: str | None, expected: str) -> bool:
    """``provided`` is ``expected``, compared in constant time over their hashes."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(_hash(provided), _hash(expected))


# -- the token from the portal -------------------------------------------------------------


@dataclass(frozen=True)
class TokenStatus:
    source: str | None  # PORTAL, ENVIRONMENT or None (no token: /mcp is closed)
    since: datetime | None  # when the portal token was made


def stored_hash(db: Session) -> Setting | None:
    return db.get(Setting, TOKEN_HASH_KEY)


def issue_token(db: Session) -> str:
    """A new token; only its hash is stored. Replaces the previous portal token."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = stored_hash(db)
    if row is None:
        db.add(Setting(key=TOKEN_HASH_KEY, value=_hash(token)))
    else:
        row.value = _hash(token)
    db.commit()
    return token


def revoke_token(db: Session) -> bool:
    """Forget the portal token; True when there was one. The environment applies again."""
    row = stored_hash(db)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def token_status(db: Session, env_token: str) -> TokenStatus:
    row = stored_hash(db)
    if row is not None:
        return TokenStatus(PORTAL, row.updated_at)
    return TokenStatus(ENVIRONMENT if env_token else None, None)


def token_valid(db: Session, provided: str | None, env_token: str) -> bool:
    """``provided`` opens the API: it is the portal token, or, without one, API_TOKEN."""
    if not provided:
        return False
    row = stored_hash(db)
    if row is not None:
        return hmac.compare_digest(_hash(provided), row.value)
    return token_matches(provided, env_token)


def unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )
