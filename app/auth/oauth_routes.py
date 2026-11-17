"""The OAuth endpoints (see app/auth/oauth.py for what they do and why).

Public, without a login: the two metadata documents, registration, the token endpoint and
revocation (app/auth/routes.py lists them as public paths). ``/oauth/authorize`` is a normal
page behind the login: whoever is not logged in lands on /login and comes back here.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import oauth
from app.auth.passwords import verify_password
from app.auth.routes import (
    BUSY,
    _attempts_note,
    _Busy,
    _client_key,
    _hashing,
    _next_param,
    _page,
    _wait_message,
    current_session,
)
from app.auth.service import AuthService
from app.db import SessionLocal, get_session
from app.security import CONTENT_SECURITY_POLICY

log = logging.getLogger(__name__)

router = APIRouter()

ACCESS_DESCRIPTION = (
    "De app mag alles wat de API kan: lezen (status, posities, beslissingen, journaal, nieuws, "
    "strategie, instellingen) en op verzoek handelen (noodstop, orders, strategie, plan, "
    "instellingen). Gevoelige instellingen en live orders blijven om je wachtwoord vragen."
)
AUTHORIZE_PARAMS = (
    "client_id",
    "redirect_uri",
    "response_type",
    "code_challenge",
    "code_challenge_method",
    "scope",
    "state",
    "resource",
)


def issuer(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _error(exc: oauth.OAuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if exc.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(exc.body(), status_code=exc.status, headers=headers)


# -- discovery -------------------------------------------------------------------------------


@router.get(oauth.RESOURCE_METADATA_PATH, include_in_schema=False)
@router.get(oauth.RESOURCE_METADATA_PATH + oauth.MCP_PATH, include_in_schema=False)
async def protected_resource_metadata(request: Request) -> Response:
    return JSONResponse(
        oauth.resource_metadata(issuer(request)), headers={"Cache-Control": "max-age=3600"}
    )


@router.get(oauth.SERVER_METADATA_PATH, include_in_schema=False)
async def authorization_server_metadata(request: Request) -> Response:
    return JSONResponse(
        oauth.server_metadata(issuer(request)), headers={"Cache-Control": "max-age=3600"}
    )


# -- registration ----------------------------------------------------------------------------


@router.post(oauth.REGISTER_PATH, include_in_schema=False)
async def register(request: Request, db: Session = Depends(get_session)) -> Response:
    try:
        metadata = await request.json()
    except ValueError:
        return _error(oauth.OAuthError("invalid_client_metadata", "ongeldige JSON"))
    if not isinstance(metadata, dict):
        return _error(oauth.OAuthError("invalid_client_metadata", "een JSON-object verwacht"))
    try:
        client = oauth.register_client(db, metadata)
    except oauth.OAuthError as exc:
        log.warning("OAuth registration refused from %s: %s", _client_key(request), exc.description)
        return _error(exc)
    return _json(oauth.client_registration(client), 201)


# -- consent ---------------------------------------------------------------------------------


def _consent_page(request: Request, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("req", None)
    ctx.setdefault("params", {})
    ctx.setdefault("access", ACCESS_DESCRIPTION)
    response = _page(request, "auth/consent.html", status=status, **ctx)
    if ctx["req"] is not None:
        # The browser applies this page's form-action also to the redirect that follows the
        # POST (Chrome does); without the app's origin the way back to Claude is blocked and
        # the page hangs on "Bezig…".
        response.headers["Content-Security-Policy"] = consent_csp(ctx["req"].redirect_uri)
    return response


def consent_csp(redirect_uri: str) -> str:
    parts = urlsplit(redirect_uri)
    origin = f"{parts.scheme}://{parts.netloc}"
    return CONTENT_SECURITY_POLICY.replace("form-action 'self'", f"form-action 'self' {origin}")


def _client_redirect(redirect_uri: str, **params: str | None) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    joiner = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{joiner}{query}", status_code=303)


def _owner(request: Request, db: Session):
    """(auth, user): auth None without the login, user None without a complete session."""
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return None, None
    user, session = current_session(request, auth, db)
    if user is None or session is None or not session.complete:
        return auth, None
    return auth, user


def _checked(request: Request, db: Session, params: dict):
    """The validated request, or the response that ends it (an error page or redirect)."""
    try:
        return oauth.check_authorize(db, params, issuer(request)), None
    except oauth.RedirectError as exc:
        return None, _client_redirect(
            exc.redirect_uri,
            error=exc.error,
            error_description=exc.description,
            state=params.get("state"),
        )
    except oauth.OAuthError as exc:
        return None, _consent_page(request, 400, error=exc.description)


@router.get(oauth.AUTHORIZE_PATH, response_class=HTMLResponse, include_in_schema=False)
async def authorize_page(request: Request, db: Session = Depends(get_session)) -> Response:
    auth, user = _owner(request, db)
    if auth is None:
        return _consent_page(
            request,
            400,
            error="De login staat uit (AUTH_ENABLED=false): /mcp is dan open, koppelen is "
            "niet nodig.",
        )
    if user is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={_next_param(target)}", status_code=303)
    params = {k: v for k, v in request.query_params.items() if k in AUTHORIZE_PARAMS}
    req, ended = _checked(request, db, params)
    if ended is not None:
        return ended
    return _consent_page(request, req=req, params=params)


@router.post(oauth.AUTHORIZE_PATH, response_class=HTMLResponse, include_in_schema=False)
async def authorize_decide(request: Request, db: Session = Depends(get_session)) -> Response:
    auth, user = _owner(request, db)
    if auth is None or user is None:
        return RedirectResponse("/login?next=/account", status_code=303)
    form = await _form(request)
    params = {k: form[k] for k in AUTHORIZE_PARAMS if form.get(k)}
    req, ended = _checked(request, db, params)
    if ended is not None:
        return ended
    assert req is not None
    if form.get("decision") != "allow":
        log.info("OAuth access denied by the owner for %s", req.client.name)
        return _client_redirect(req.redirect_uri, error="access_denied", state=req.state)

    def failed(status: int, error: str) -> HTMLResponse:
        return _consent_page(request, status, req=req, params=params, error=error)

    # The same password check as /account: a grant is full API access for a month.
    key = _client_key(request)
    wait = auth.throttle.blocked_for(db, key)
    if wait:
        return failed(429, _wait_message(wait))
    try:
        ok = await _hashing(verify_password, form.get("password") or "", user.password_hash)
    except _Busy:
        return failed(429, BUSY)
    if not ok:
        auth.throttle.record_failure(db, key)
        log.warning("wrong password on /oauth/authorize from %s", key)
        return failed(401, "Het wachtwoord klopt niet." + _attempts_note(auth, db, key))
    auth.throttle.reset(db, key)
    code = oauth.issue_code(db, req)
    log.warning("OAuth access allowed for %s by %s from %s", req.client.name, user.username, key)
    return _client_redirect(req.redirect_uri, code=code, state=req.state)


# -- tokens ----------------------------------------------------------------------------------


async def _form(request: Request) -> dict[str, str]:
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - a bad body is the client's problem, not a 500
        return {}
    return {k: v for k, v in form.items() if isinstance(v, str)}


@router.post(oauth.TOKEN_PATH, include_in_schema=False)
async def token(request: Request) -> Response:
    form = await _form(request)
    grant_type = form.get("grant_type")
    try:
        with SessionLocal() as db:
            if grant_type == "authorization_code":
                grant = oauth.exchange_code(db, form, issuer(request))
            elif grant_type == "refresh_token":
                grant = oauth.refresh_grant(db, form)
            else:
                raise oauth.OAuthError(
                    "unsupported_grant_type", "alleen authorization_code en refresh_token"
                )
    except oauth.OAuthError as exc:
        log.warning("OAuth token refused from %s: %s", _client_key(request), exc.description)
        return _error(exc)
    return _json(grant.body())


@router.post(oauth.REVOKE_PATH, include_in_schema=False)
async def revoke(request: Request) -> Response:
    form = await _form(request)
    with SessionLocal() as db:
        if oauth.revoke_token(db, form.get("token")):
            log.warning("OAuth token revoked through /oauth/revoke from %s", _client_key(request))
    return Response(status_code=200, headers={"Cache-Control": "no-store"})  # RFC 7009: always
