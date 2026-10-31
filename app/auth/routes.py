"""Setup and login pages, second-factor enrolment, and the middleware that protects the rest."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.service import AuthService, validate_new_password
from app.auth.session import COOKIE_NAME, SessionData
from app.db import SessionLocal, get_session
from app.web import templates

log = logging.getLogger("crypie.auth")

router = APIRouter()

# Reachable without a session. Everything else needs a complete (password + code) login.
PUBLIC_PATHS = {"/health", "/setup", "/login", "/login/mfa", "/logout"}
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
    if parts.path.startswith(("/login", "/setup")) or parts.path == "/logout":
        return "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", request.app.state.settings)
    ctx.setdefault("error", None)
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.url.path.startswith("/api/") or (
        "application/json" in accept and "text/html" not in accept
    )


# -- middleware -------------------------------------------------------------------------


async def require_login(request: Request, call_next):
    """Send visitors to /setup (no account yet) or /login; answer 401/503 JSON for the API."""
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    path = request.url.path
    request.state.user = None
    if auth is None or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        if auth is not None and (session := current_session(request)) and session.complete:
            request.state.user = session.username
        return await call_next(request)
    with SessionLocal() as db:
        account = auth.account(db)
    if account is None:
        if _wants_json(request):
            return JSONResponse({"detail": "Nog geen account; maak het aan op /setup"}, 503)
        return RedirectResponse("/setup", status_code=303)
    session = current_session(request)
    if session is not None and session.username != account.username:
        session = None  # cookie from a deleted or renamed account
    if session is not None and session.complete:
        request.state.user = session.username
        return await call_next(request)
    if session is not None and path in STAGE_ONE_PATHS:
        return await call_next(request)
    if _wants_json(request):
        return JSONResponse({"detail": "Niet ingelogd"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={target}" if target != "/" else "/login", status_code=303)


# -- first run: create the account ---------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request, auth: AuthService = Depends(get_auth), db: Session = Depends(get_session)
):
    if auth.has_account(db):
        return RedirectResponse("/login", status_code=303)
    return _page(request, "auth/setup.html", username="")


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if auth.has_account(db):
        return RedirectResponse("/login", status_code=303)
    username = username.strip()
    error = None
    if not 2 <= len(username) <= 64:
        error = "Kies een gebruikersnaam van 2 tot 64 tekens."
    else:
        error = validate_new_password(password, password_repeat)
    if error:
        return _page(request, "auth/setup.html", status=400, username=username, error=error)
    user = auth.create_account(db, username, password)
    log.info("account %r created at /setup from %s", user.username, _client_key(request))
    response = RedirectResponse("/login/mfa/setup", status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.stage_one_token(user.username), 10 * 60)
    return response


# -- step 1: password -------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if not auth.has_account(db):
        return RedirectResponse("/setup", status_code=303)
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
    if not auth.has_account(db):
        return RedirectResponse("/setup", status_code=303)
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
    user = auth.check_password(db, username, password)
    if user is None:
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
    where = "/login/mfa" if user.mfa_enrolled else "/login/mfa/setup"
    response = RedirectResponse(f"{where}?next={target}", status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.stage_one_token(user.username), 10 * 60)
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
            log.warning("recovery code used for %s from %s", session.username, key)
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
    _set_cookie(
        response, request, COOKIE_NAME, auth.full_token(session.username), auth.session_seconds
    )
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
    response = _render_setup(request, auth, db, secret, _safe_next(next), reenrol)
    # The candidate secret travels in a signed cookie until the user confirms it with a code.
    token = auth.signer.encode(secret, mfa=False, max_age=15 * 60)
    _set_cookie(response, request, SETUP_SECRET_COOKIE, token, max_age=15 * 60)
    return response


def _render_setup(
    request: Request,
    auth: AuthService,
    db: Session,
    secret: str,
    next: str,
    reenrol: bool,
    **ctx,
) -> HTMLResponse:
    uri = totp.provisioning_uri(secret, auth.user(db).username, issuer="Crypie")
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
            db,
            secret,
            _safe_next(next),
            reenrol,
            status=400,
            error="De code klopt niet. Controleer de tijd op je telefoon.",
        )
    auth.throttle.reset(_client_key(request))
    log.info("authenticator enrolled for %s", session.username)
    response = _page(
        request, "auth/recovery_codes.html", codes=codes, next=_safe_next(next), fresh=True
    )
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _set_cookie(
        response, request, COOKIE_NAME, auth.full_token(session.username), auth.session_seconds
    )
    return response


# -- account -----------------------------------------------------------------------------


def _account_page(
    request: Request, auth: AuthService, db: Session, status: int = 200, **ctx
) -> HTMLResponse:
    ctx.setdefault("message", None)
    return _page(
        request,
        "auth/account.html",
        status=status,
        user=auth.user(db),
        codes_left=auth.recovery_codes_left(db),
        **ctx,
    )


@router.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request, auth: AuthService = Depends(get_auth), db: Session = Depends(get_session)
):
    return _account_page(request, auth, db)


@router.post("/account", response_class=HTMLResponse)
async def account_action(
    request: Request,
    action: str = Form(...),
    password: str = Form(""),
    new_password: str = Form(""),
    new_password_repeat: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    """Sensitive changes ask for the current password again, even inside a valid session."""
    key = _client_key(request)
    user = auth.user(db)
    if auth.throttle.blocked_for(key) or auth.check_password(db, user.username, password) is None:
        auth.throttle.record_failure(key)
        return _account_page(request, auth, db, status=401, error="Het wachtwoord klopt niet.")
    auth.throttle.reset(key)
    if action == "recovery":
        codes = auth.issue_recovery_codes(db)
        log.info("new recovery codes issued for %s", user.username)
        return _page(request, "auth/recovery_codes.html", codes=codes, next="/account", fresh=False)
    if action == "reenrol":
        return RedirectResponse("/login/mfa/setup?next=/account", status_code=303)
    if action == "password":
        error = validate_new_password(new_password, new_password_repeat)
        if error:
            return _account_page(request, auth, db, status=400, error=error)
        auth.change_password(db, new_password)
        log.info("password changed for %s", user.username)
        return _account_page(request, auth, db, message="Je wachtwoord is gewijzigd.")
    return RedirectResponse("/account", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    _clear_cookie(response, request, COOKIE_NAME)
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    return response
