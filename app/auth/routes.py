"""Login pages, second-factor enrolment and the middleware that protects every other page."""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.service import AuthService
from app.auth.session import COOKIE_NAME, SessionData
from app.db import get_session
from app.web import templates

log = logging.getLogger("crypie.auth")

router = APIRouter()

# Reachable without a session. Everything else needs a complete (password + code) login.
PUBLIC_PATHS = {"/health", "/login", "/login/mfa", "/logout"}
PUBLIC_PREFIXES = ("/static/",)
# Reachable with a password-only session, because they finish the login.
STAGE_ONE_PATHS = {"/login/mfa", "/login/mfa/setup"}

SETUP_SECRET_COOKIE = "crypie_mfa_setup"


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def current_session(request: Request) -> SessionData | None:
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return None
    return auth.signer.decode(request.cookies.get(COOKIE_NAME))


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _set_cookie(response: Response, request: Request, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, request: Request, name: str) -> None:
    response.delete_cookie(name, path="/", httponly=True, secure=_secure(request), samesite="lax")


def _safe_next(value: str | None) -> str:
    """Only redirect to a path on this site, never to another host."""
    if not value:
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return "/"
    if parts.path.startswith("//"):
        return "/"
    if parts.path.startswith("/login") or parts.path == "/logout":
        return "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", request.app.state.settings)
    ctx.setdefault("error", None)
    return templates.TemplateResponse(request, name, ctx, status_code=status)


# -- middleware -------------------------------------------------------------------------


async def require_login(request: Request, call_next):
    """Redirect to /login (or answer 401 for the API) unless the session is complete."""
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    path = request.url.path
    request.state.user = None
    if auth is None or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        if auth is not None and (session := current_session(request)) and session.complete:
            request.state.user = session.username
        return await call_next(request)
    if not auth.configured:
        return _page(request, "auth/not_configured.html", status=503)
    session = current_session(request)
    if session is not None and session.complete:
        request.state.user = session.username
        return await call_next(request)
    if session is not None and path in STAGE_ONE_PATHS:
        return await call_next(request)
    accept = request.headers.get("accept", "")
    if path.startswith("/api/") or ("application/json" in accept and "text/html" not in accept):
        return JSONResponse({"detail": "Niet ingelogd"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={target}" if target != "/" else "/login", status_code=303)


# -- step 1: password -------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", auth: AuthService = Depends(get_auth)):
    if not auth.configured:
        return _page(request, "auth/not_configured.html", status=503)
    session = current_session(request)
    if session is not None and session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page(request, "auth/login.html", next=_safe_next(next), username="")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if not auth.configured:
        return _page(request, "auth/not_configured.html", status=503)
    key = _client_key(request)
    wait = auth.throttle.blocked_for(key)
    if wait:
        return _page(
            request,
            "auth/login.html",
            status=429,
            next=_safe_next(next),
            username=username,
            error=f"Te veel mislukte pogingen. Probeer het over {max(wait, 1)} seconden opnieuw.",
        )
    if not auth.check_password(username, password):
        auth.throttle.record_failure(key)
        log.warning("failed login for %r from %s", username, key)
        return _page(
            request,
            "auth/login.html",
            status=401,
            next=_safe_next(next),
            username=username,
            error="Gebruikersnaam of wachtwoord klopt niet.",
        )
    target = _safe_next(next)
    enrolled = auth.user(db).mfa_enrolled
    where = "/login/mfa" if enrolled else "/login/mfa/setup"
    response = RedirectResponse(f"{where}?next={target}", status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.stage_one_token(), max_age=10 * 60)
    return response


# -- step 2: authenticator code ------------------------------------------------------------


@router.get("/login/mfa", response_class=HTMLResponse)
async def mfa_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    session = current_session(request)
    if session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if not auth.user(db).mfa_enrolled:
        return RedirectResponse(f"/login/mfa/setup?next={_safe_next(next)}", status_code=303)
    return _page(request, "auth/mfa.html", next=_safe_next(next))


@router.post("/login/mfa", response_class=HTMLResponse)
async def mfa_submit(
    request: Request,
    code: str = Form(""),
    next: str = Form("/"),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    session = current_session(request)
    if session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    key = _client_key(request)
    wait = auth.throttle.blocked_for(key)
    if wait:
        return _page(
            request,
            "auth/mfa.html",
            status=429,
            next=_safe_next(next),
            error=f"Te veel mislukte pogingen. Probeer het over {max(wait, 1)} seconden opnieuw.",
        )
    cleaned = code.strip()
    ok = False
    if cleaned.replace(" ", "").isdigit():
        ok = auth.check_totp(db, cleaned)
    elif cleaned:
        ok = auth.use_recovery_code(db, cleaned)
        if ok:
            log.warning("recovery code used for %s from %s", auth.username, key)
    if not ok:
        auth.throttle.record_failure(key)
        return _page(
            request,
            "auth/mfa.html",
            status=401,
            next=_safe_next(next),
            error="De code klopt niet. Controleer de tijd op je telefoon en probeer het opnieuw.",
        )
    auth.throttle.reset(key)
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.full_token(), max_age=auth.session_seconds)
    return response


# -- enrolment ---------------------------------------------------------------------------


@router.get("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    session = current_session(request)
    if session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    reenrol = session.complete
    if auth.user(db).mfa_enrolled and not reenrol:
        return RedirectResponse(f"/login/mfa?next={_safe_next(next)}", status_code=303)
    secret = totp.new_secret()
    response = _render_setup(request, auth, secret, _safe_next(next), reenrol)
    # The candidate secret travels in a signed cookie until the user confirms it with a code.
    token = auth.signer.encode(secret, mfa=False, max_age=15 * 60)
    _set_cookie(response, request, SETUP_SECRET_COOKIE, token, max_age=15 * 60)
    return response


def _render_setup(
    request: Request, auth: AuthService, secret: str, next: str, reenrol: bool, **ctx
) -> HTMLResponse:
    uri = totp.provisioning_uri(secret, auth.username, issuer="Crypie")
    qr = segno.make(uri, error="m").svg_inline(scale=5, border=1, dark="#000", light=None)
    return _page(
        request,
        "auth/mfa_setup.html",
        qr_svg=qr,
        secret_pretty=totp.pretty_secret(secret),
        next=next,
        reenrol=reenrol,
        **ctx,
    )


@router.post("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_submit(
    request: Request,
    code: str = Form(""),
    next: str = Form("/"),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    session = current_session(request)
    if session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    reenrol = session.complete
    if auth.user(db).mfa_enrolled and not reenrol:
        return RedirectResponse(f"/login/mfa?next={_safe_next(next)}", status_code=303)
    candidate = auth.signer.decode(request.cookies.get(SETUP_SECRET_COOKIE))
    if candidate is None:
        return RedirectResponse(f"/login/mfa/setup?next={_safe_next(next)}", status_code=303)
    secret = candidate.username
    codes = auth.enrol_totp(db, secret, code)
    if codes is None:
        return _render_setup(
            request,
            auth,
            secret,
            _safe_next(next),
            reenrol,
            status=400,
            error="De code klopt niet. Controleer de tijd op je telefoon.",
        )
    auth.throttle.reset(_client_key(request))
    log.info("authenticator enrolled for %s", auth.username)
    response = _page(
        request, "auth/recovery_codes.html", codes=codes, next=_safe_next(next), fresh=True
    )
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _set_cookie(response, request, COOKIE_NAME, auth.full_token(), max_age=auth.session_seconds)
    return response


# -- account -----------------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
    message: str | None = None,
):
    user = auth.user(db)
    return _page(
        request,
        "auth/account.html",
        user=user,
        codes_left=auth.recovery_codes_left(db),
        message=message,
    )


@router.post("/account", response_class=HTMLResponse)
async def account_action(
    request: Request,
    action: str = Form(...),
    password: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    """Sensitive changes ask for the password again, even inside a valid session."""
    key = _client_key(request)
    if auth.throttle.blocked_for(key) or not auth.check_password(auth.username, password):
        auth.throttle.record_failure(key)
        return _page(
            request,
            "auth/account.html",
            status=401,
            user=auth.user(db),
            codes_left=auth.recovery_codes_left(db),
            message=None,
            error="Het wachtwoord klopt niet.",
        )
    auth.throttle.reset(key)
    if action == "recovery":
        codes = auth.issue_recovery_codes(db)
        log.info("new recovery codes issued for %s", auth.username)
        return _page(request, "auth/recovery_codes.html", codes=codes, next="/account", fresh=False)
    if action == "reenrol":
        return RedirectResponse("/login/mfa/setup?next=/account", status_code=303)
    return RedirectResponse("/account", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    _clear_cookie(response, request, COOKIE_NAME)
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    return response


def new_session_secret() -> str:
    return secrets.token_urlsafe(32)
