"""A bearer token for the JSON API and the MCP endpoint, next to the browser login.

``API_TOKEN`` (environment or ``/run/secrets/api_token``) lets a program call ``/api/*`` and
``/mcp`` with ``Authorization: Bearer <token>``: that is how Claude's MCP connection
(app/mcp_server.py) gets in, without a password, an authenticator code or a cookie. The token
only counts on those paths; a page keeps asking for the login, so a token in a browser opens
nothing. Sensitive changes still ask the current password (step-up), also with the token.

The comparison is constant time over the SHA-256 of both sides (a wrong token of another length
takes as long as one of the right length). With fewer than ``API_TOKEN_MIN_LENGTH`` characters
the token is ignored (app/config.py warns), so a weak token never opens anything.
"""

from __future__ import annotations

import hmac
import logging
from hashlib import sha256

from fastapi import Request
from starlette.responses import JSONResponse

from app.auth.routes import require_login
from app.config import API_TOKEN_MIN_LENGTH

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = API_TOKEN_MIN_LENGTH
MCP_PATH = "/mcp"
API_PREFIX = "/api/"
BEARER = "bearer "


def token_path(path: str) -> bool:
    """Paths on which the token counts: the JSON API and the MCP endpoint."""
    return path == MCP_PATH or path.startswith(API_PREFIX)


def bearer_token(request: Request) -> str | None:
    """The token of an ``Authorization: Bearer ...`` header, or None without one."""
    header = request.headers.get("authorization", "")
    if header[: len(BEARER)].lower() != BEARER:
        return None
    return header[len(BEARER) :].strip()


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        sha256(provided.encode()).digest(), sha256(expected.encode()).digest()
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )


async def require_login_or_token(request: Request, call_next):
    """The login middleware (app/auth/routes.py), with the API token as a second door.

    With the login on: a valid bearer token opens ``/api/*`` and ``/mcp``; a wrong one answers
    401 (and is logged); ``/mcp`` without a token answers 401 too, so the MCP endpoint never
    works on a browser session. Everything else goes to ``require_login`` unchanged. With the
    login off (AUTH_ENABLED=false) nothing is checked, as everywhere in the app.
    """
    auth = getattr(request.app.state, "auth", None)
    path = request.url.path
    if auth is not None and token_path(path):
        provided = bearer_token(request)
        if provided is not None:
            expected = getattr(request.app.state.env, "api_token", "")
            if token_matches(provided, expected):
                request.state.user = "api-token"
                request.state.api_token = True
                return await call_next(request)
            log.warning("wrong API token on %s from %s", path, _client(request))
            return _unauthorized("API-token klopt niet")
        if path == MCP_PATH:
            return _unauthorized("Niet ingelogd: /mcp vraagt een API-token (API_TOKEN)")
    return await require_login(request, call_next)
