"""Setup and login pages, second-factor enrolment, and the middleware that protects the rest."""

from __future__ import annotations

import functools
import ipaddress
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import totp
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.service import (
    MFA_SETUP_SECONDS,
    REAUTH_SECONDS,
    STAGE_ONE_SECONDS,
    AuthService,
    validate_new_password,
)
from app.auth.session import COOKIE_NAME, LOGIN, REAUTH, SESSION, SessionData, cookie_name
from app.db import AuthUser, SessionLocal, get_session
from app.web import templates

log = logging.getLogger("crypie.auth")

router = APIRouter()

# Reachable without a session. Everything else needs a complete (password + code) login.
PUBLIC_PATHS = {"/health", "/setup", "/login", "/login/mfa", "/logout"}
PUBLIC_PREFIXES = ("/static/",)
# Reachable with a password-only session, because they finish the login.
STAGE_ONE_PATHS = {"/login/mfa", "/login/mfa/setup"}

SETUP_SECRET_COOKIE = "crypie_mfa_setup"
REAUTH_COOKIE = "crypie_reauth"

# PBKDF2 runs in worker threads, at most this many at once; more concurrent attempts get a 429
# straight away instead of queueing up (and an attacker cannot stall the trading loop).
HASH_CONCURRENCY = 2
_HASH_SLOTS = threading.BoundedSemaphore(HASH_CONCURRENCY)

BUSY = "Het is even druk op de server. Probeer het over een paar seconden opnieuw."


class _Busy(Exception):
    """All password-hashing slots are taken."""


async def _hashing(fn, *args):
    """Run a PBKDF2 call off the event loop, or raise _Busy when every slot is in use."""
    if not _HASH_SLOTS.acquire(blocking=False):
        raise _Busy
    try:
        return await run_in_threadpool(fn, *args)
    finally:
        _HASH_SLOTS.release()


@functools.cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _verify(password: str, stored: str | None) -> bool:
    # Without an account, still spend the same time hashing: no timing hint either way.
    return verify_password(password, stored or _dummy_hash())


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


def _cookie(request: Request, base: str) -> str:
    return cookie_name(base, _secure(request))


def _read_cookie(request: Request, base: str) -> str | None:
    return request.cookies.get(_cookie(request, base))


def _set_cookie(response: Response, request: Request, base: str, value: str, max_age: int) -> None:
    response.set_cookie(
        _cookie(request, base),
        value,
        max_age=max(1, max_age),
        httponly=True,
        secure=_secure(request),
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, request: Request, base: str) -> None:
    response.delete_cookie(
        _cookie(request, base), path="/", httponly=True, secure=_secure(request), samesite="lax"
    )


def _decode_session(request: Request, auth: AuthService) -> SessionData | None:
    """The login cookie: a complete session or a password-only (stage one) one."""
    token = _read_cookie(request, COOKIE_NAME)
    return auth.signer.decode(token, purpose=SESSION) or auth.signer.decode(token, purpose=LOGIN)


def current_session(
    request: Request, auth: AuthService, db: Session
) -> tuple[AuthUser | None, SessionData | None]:
    """The account plus the cookie's session, or (account, None) when the cookie does not fit
    this account (forged, expired, revoked, or from a deleted account)."""
    user = auth.account(db)
    data = _decode_session(request, auth)
    return user, data if auth.session_valid(user, data) else None


def _safe_next(value: str | None) -> str:
    """Only redirect to a path on this site, never to another host."""
    if not value:
        return "/"
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
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
    """Throttle key for the client address; IPv6 per /64, since one host usually owns that."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        safe = "".join(c for c in host[:64] if c.isascii() and (c.isalnum() or c in ".:-_"))
        return "ip:" + (safe or "unknown")
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return f"ip:{ip.ipv4_mapped}"
        return "ip:" + str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return f"ip:{ip}"


def _account_key(user: AuthUser) -> str:
    """Throttle key for the second factor of the account (only reachable after the password)."""
    return f"mfa:{user.account_id}"


def _wait_message(wait: int) -> str:
    return f"Te veel mislukte pogingen. Probeer het over {max(wait, 1)} seconden opnieuw."


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
    """Send visitors to /login; answer 401 JSON for the API.

    Without an account visitors also go to /login: whether the instance is still unclaimed is
    not given away. The owner creates the account with the /setup link from the server log.
    """
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    path = request.url.path
    request.state.user = None
    if auth is None or path == "/health" or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    with SessionLocal() as db:
        _, session = current_session(request, auth, db)
    if path in PUBLIC_PATHS:
        if session is not None and session.complete:
            request.state.user = session.username
        return await call_next(request)
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


def _setup_gate(
    request: Request, auth: AuthService, db: Session, token: str
) -> HTMLResponse | None:
    """None when ``token`` is the setup token, else the page that says where to find it.

    The page looks the same whether or not an account exists, so probing /setup does not tell
    whether this instance is still unclaimed.
    """
    key = _client_key(request)
    wait = auth.throttle.blocked_for(db, key)
    if wait:
        return _page(request, "auth/setup_token.html", status=429, error=_wait_message(wait))
    if token and secrets.compare_digest(token.encode(), auth.setup_token.encode()):
        return None
    if not auth.has_account(db):
        auth.log_setup_hint()
    if token:
        auth.throttle.record_failure(db, key)
        log.warning("wrong setup token from %s", key)
        return _page(
            request,
            "auth/setup_token.html",
            status=403,
            error="Deze setup-link klopt niet (meer). Neem de link uit de serverlogs.",
        )
    return _page(request, "auth/setup_token.html", status=404)


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    token: str = "",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if (gate := _setup_gate(request, auth, db, token)) is not None:
        return gate
    if auth.has_account(db):
        return RedirectResponse("/login", status_code=303)
    return _page(request, "auth/setup.html", username="", token=token)


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    token: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if (gate := _setup_gate(request, auth, db, token)) is not None:
        return gate
    if auth.has_account(db):
        return RedirectResponse("/login", status_code=303)
    username = username.strip()
    error = None
    if not 2 <= len(username) <= 64:
        error = "Kies een gebruikersnaam van 2 tot 64 tekens."
    else:
        error = validate_new_password(password, password_repeat)
    if error:
        return _page(
            request, "auth/setup.html", status=400, username=username, token=token, error=error
        )
    try:
        hashed = await _hashing(hash_password, password)
    except _Busy:
        return _page(
            request, "auth/setup.html", status=429, username=username, token=token, error=BUSY
        )
    if auth.has_account(db):  # someone else finished first while we were hashing
        return RedirectResponse("/login", status_code=303)
    user = auth.create_account(db, username, password_hash=hashed)
    auth.throttle.reset(db, _client_key(request))
    log.warning("account %r created at /setup from %s", user.username, _client_key(request))
    response = RedirectResponse("/login/mfa/setup", status_code=303)
    # Having had the setup token is the proof that allows linking the first authenticator.
    _set_cookie(
        response, request, COOKIE_NAME, auth.stage_one_token(user, enrol=True), STAGE_ONE_SECONDS
    )
    return response


# -- step 1: password -------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    _, session = current_session(request, auth, db)
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
    key = _client_key(request)
    shown = username[:64]

    def again(status: int, error: str) -> HTMLResponse:
        return _page(
            request,
            "auth/login.html",
            status=status,
            next=_safe_next(next),
            username=shown,
            error=error,
        )

    wait = auth.throttle.blocked_for(db, key)
    if wait:
        return again(429, _wait_message(wait))
    user = auth.account(db)
    try:
        ok = await _hashing(_verify, password, user.password_hash if user else None)
    except _Busy:
        return again(429, BUSY)
    if user is None or not ok or not auth.username_matches(user, username):
        auth.throttle.record_failure(db, key)
        log.warning("failed login from %s", key)
        return again(401, "Gebruikersnaam of wachtwoord klopt niet.")
    if needs_rehash(user.password_hash):
        try:
            auth.store_rehash(db, user, await _hashing(hash_password, password))
            log.info("password hash upgraded for %s", user.username)
        except _Busy:
            pass  # next time
    target = _safe_next(next)
    where = "/login/mfa" if user.mfa_enrolled else "/login/mfa/setup"
    response = RedirectResponse(f"{where}?next={target}", status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.stage_one_token(user), STAGE_ONE_SECONDS)
    return response


# -- step 2: authenticator code ------------------------------------------------------------


def _second_factor_wait(request: Request, auth: AuthService, db: Session, user: AuthUser) -> int:
    return max(
        auth.throttle.blocked_for(db, _client_key(request)),
        auth.throttle.blocked_for(db, _account_key(user)),
    )


def _second_factor_failed(request: Request, auth: AuthService, db: Session, user: AuthUser):
    auth.throttle.record_failure(db, _client_key(request))
    auth.throttle.record_failure(db, _account_key(user))
    log.warning("wrong second factor from %s", _client_key(request))


@router.get("/login/mfa", response_class=HTMLResponse)
async def mfa_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    user, session = current_session(request, auth, db)
    if user is None or session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if not user.mfa_enrolled:
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
    user, session = current_session(request, auth, db)
    if user is None or session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    wait = _second_factor_wait(request, auth, db, user)
    if wait:
        return _page(
            request, "auth/mfa.html", status=429, next=_safe_next(next), error=_wait_message(wait)
        )
    kind = auth.check_second_factor(db, code)
    if kind is None:
        _second_factor_failed(request, auth, db, user)
        return _page(
            request,
            "auth/mfa.html",
            status=401,
            next=_safe_next(next),
            error="De code klopt niet. Controleer de tijd op je telefoon en probeer het opnieuw.",
        )
    if kind == "recovery":
        log.warning("recovery code used for %s from %s", user.username, _client_key(request))
    failures = auth.throttle.failures(db, _account_key(user))
    if failures:
        log.warning(
            "%d wrong second-factor code(s) before this login of %s", failures, user.username
        )
    auth.note_login_failures(db, failures)
    auth.throttle.reset(db, _client_key(request))
    auth.throttle.reset(db, _account_key(user))
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.full_token(user), auth.session_seconds)
    return response


# -- enrolment ---------------------------------------------------------------------------


def _render_setup(
    request: Request,
    user: AuthUser,
    secret: str,
    next: str,
    reenrol: bool,
    status: int = 200,
    **ctx,
) -> HTMLResponse:
    uri = totp.provisioning_uri(secret, user.username, issuer="Crypie")
    qr = segno.make(uri, error="m").svg_inline(scale=5, border=1, dark="#000", light=None)
    return _page(
        request,
        "auth/mfa_setup.html",
        status=status,
        qr_svg=qr,
        secret_pretty=totp.pretty_secret(secret),
        next=next,
        reenrol=reenrol,
        action="/account/mfa" if reenrol else "/login/mfa/setup",
        **ctx,
    )


def _new_candidate(
    request: Request, auth: AuthService, user: AuthUser, next: str, reenrol: bool
) -> HTMLResponse:
    secret = totp.new_secret()
    response = _render_setup(request, user, secret, next, reenrol)
    # The candidate secret travels in a signed cookie until the user confirms it with a code.
    _set_cookie(
        response,
        request,
        SETUP_SECRET_COOKIE,
        auth.setup_secret_token(user, secret),
        MFA_SETUP_SECONDS,
    )
    return response


@router.get("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_page(
    request: Request,
    next: str = "/",
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    """First enrolment only; linking a new authenticator later goes through /account."""
    user, session = current_session(request, auth, db)
    if user is None or session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if user.mfa_enrolled:
        return RedirectResponse(f"/login/mfa?next={_safe_next(next)}", status_code=303)
    if not session.extra.get("enrol"):
        return _page(request, "auth/enrol_token.html", next=_safe_next(next))
    return _new_candidate(request, auth, user, _safe_next(next), reenrol=False)


@router.post("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_submit(
    request: Request,
    code: str = Form(""),
    enrol_token: str = Form(""),
    next: str = Form("/"),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    user, session = current_session(request, auth, db)
    if user is None or session is None:
        return RedirectResponse(f"/login?next={_safe_next(next)}", status_code=303)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if user.mfa_enrolled:
        return RedirectResponse(f"/login/mfa?next={_safe_next(next)}", status_code=303)
    if not session.extra.get("enrol"):
        return _check_enrol_token(request, auth, db, user, enrol_token, _safe_next(next))
    secret = auth.setup_secret(user, _read_cookie(request, SETUP_SECRET_COOKIE))
    if secret is None:
        return RedirectResponse(f"/login/mfa/setup?next={_safe_next(next)}", status_code=303)
    codes = auth.enrol_totp(db, secret, code)
    if codes is None:
        return _render_setup(
            request,
            user,
            secret,
            _safe_next(next),
            False,
            status=400,
            error="De code klopt niet. Controleer de tijd op je telefoon.",
        )
    auth.throttle.reset(db, _client_key(request))
    log.info("authenticator enrolled for %s", user.username)
    response = _page(
        request, "auth/recovery_codes.html", codes=codes, next=_safe_next(next), fresh=True
    )
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _set_cookie(response, request, COOKIE_NAME, auth.full_token(user), auth.session_seconds)
    return response


def _check_enrol_token(
    request: Request, auth: AuthService, db: Session, user: AuthUser, token: str, next: str
) -> Response:
    """After ``reset-mfa`` only the one-time code it printed allows linking an authenticator."""
    wait = _second_factor_wait(request, auth, db, user)
    if wait:
        return _page(
            request, "auth/enrol_token.html", status=429, next=next, error=_wait_message(wait)
        )
    if not token.strip():
        return _page(
            request, "auth/enrol_token.html", status=400, next=next, error="Vul de code in."
        )
    if not auth.check_enrol_token(db, token):
        _second_factor_failed(request, auth, db, user)
        return _page(
            request,
            "auth/enrol_token.html",
            status=401,
            next=next,
            error="Deze code klopt niet of is verlopen. Maak op de server een nieuwe aan.",
        )
    response = RedirectResponse(f"/login/mfa/setup?next={next}", status_code=303)
    _set_cookie(
        response, request, COOKIE_NAME, auth.stage_one_token(user, enrol=True), STAGE_ONE_SECONDS
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


def _reissue_session(
    response: Response, request: Request, auth: AuthService, user: AuthUser, old: SessionData
) -> None:
    """Keep this browser logged in after a revocation, without extending its lifetime."""
    _set_cookie(
        response,
        request,
        COOKIE_NAME,
        auth.full_token(user, expires_at=old.expires_at),
        old.expires_at - int(time.time()),
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
    code: str = Form(""),
    new_password: str = Form(""),
    new_password_repeat: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    """Sensitive changes ask for the current password again, even inside a valid session."""
    user, session = current_session(request, auth, db)
    if user is None or session is None or not session.complete:
        return RedirectResponse("/login?next=/account", status_code=303)
    if action == "logout_all":
        auth.revoke_sessions(db)
        log.warning("all sessions of %s revoked from %s", user.username, _client_key(request))
        response = RedirectResponse("/login", status_code=303)
        for name in (COOKIE_NAME, SETUP_SECRET_COOKIE, REAUTH_COOKIE):
            _clear_cookie(response, request, name)
        return response
    key = _client_key(request)
    wait = auth.throttle.blocked_for(db, key)
    if wait:
        return _account_page(request, auth, db, status=429, error=_wait_message(wait))
    try:
        ok = await _hashing(verify_password, password, user.password_hash)
    except _Busy:
        return _account_page(request, auth, db, status=429, error=BUSY)
    if not ok:
        auth.throttle.record_failure(db, key)
        log.warning("wrong password on /account from %s", key)
        return _account_page(request, auth, db, status=401, error="Het wachtwoord klopt niet.")
    auth.throttle.reset(db, key)
    if action in ("recovery", "reenrol"):
        # New recovery codes are a lasting second factor, like a new authenticator: both
        # need a current code too, so a stolen cookie plus the password is not enough.
        wait = _second_factor_wait(request, auth, db, user)
        if wait:
            return _account_page(request, auth, db, status=429, error=_wait_message(wait))
        kind = auth.check_second_factor(db, code)
        if kind is None:
            _second_factor_failed(request, auth, db, user)
            return _account_page(
                request,
                auth,
                db,
                status=401,
                error="De code klopt niet. Gebruik een code uit je authenticator-app of een "
                "herstelcode.",
            )
        auth.throttle.reset(db, _account_key(user))
        if kind == "recovery":
            log.warning("recovery code used for %s from %s", user.username, key)
    if action == "recovery":
        codes = auth.issue_recovery_codes(db)
        log.info("new recovery codes issued for %s", user.username)
        response = _page(
            request, "auth/recovery_codes.html", codes=codes, next="/account", fresh=False
        )
        _reissue_session(response, request, auth, user, session)
        return response
    if action == "reenrol":
        response = RedirectResponse("/account/mfa", status_code=303)
        _set_cookie(response, request, REAUTH_COOKIE, auth.reauth_token(user), REAUTH_SECONDS)
        return response
    if action == "password":
        error = validate_new_password(new_password, new_password_repeat)
        if error:
            return _account_page(request, auth, db, status=400, error=error)
        try:
            hashed = await _hashing(hash_password, new_password)
        except _Busy:
            return _account_page(request, auth, db, status=429, error=BUSY)
        auth.change_password(db, password_hash=hashed)
        log.warning("password changed for %s; other sessions logged out", user.username)
        response = _account_page(
            request,
            auth,
            db,
            message="Je wachtwoord is gewijzigd. Andere browsers zijn uitgelogd.",
        )
        _reissue_session(response, request, auth, user, session)
        return response
    return RedirectResponse("/account", status_code=303)


# -- step-up for sensitive changes elsewhere in the app ----------------------------------------

STEP_UP_MISSING = "Vul je huidige wachtwoord in om deze wijziging te bevestigen."
STEP_UP_WRONG = "Het wachtwoord klopt niet."


class StepUpError(Exception):
    """The current password was missing, wrong, or cannot be checked right now."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def step_up_enabled(request: Request) -> bool:
    """Without the login (AUTH_ENABLED=false) there is no password to ask for."""
    return getattr(request.app.state, "auth", None) is not None


async def require_current_password(request: Request, db: Session, password: str | None) -> None:
    """Ask for the current password again before a sensitive change, like /account does.

    Raises StepUpError (403 missing or wrong, 429 throttled or busy). Wrong passwords count
    for the client's throttle, the same one as the login and /account use.
    """
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return
    user = auth.account(db)
    if user is None:
        raise StepUpError(401, "Niet ingelogd")
    key = _client_key(request)
    wait = auth.throttle.blocked_for(db, key)
    if wait:
        raise StepUpError(429, _wait_message(wait))
    if not password:
        raise StepUpError(403, STEP_UP_MISSING)
    try:
        ok = await _hashing(verify_password, password, user.password_hash)
    except _Busy as exc:
        raise StepUpError(429, BUSY) from exc
    if not ok:
        auth.throttle.record_failure(db, key)
        log.warning("wrong password for a sensitive change on %s from %s", request.url.path, key)
        raise StepUpError(403, STEP_UP_WRONG)
    auth.throttle.reset(db, key)


def _reauth_ok(request: Request, auth: AuthService, user: AuthUser) -> bool:
    data = auth.signer.decode(_read_cookie(request, REAUTH_COOKIE), purpose=REAUTH)
    return auth.session_valid(user, data)


@router.get("/account/mfa", response_class=HTMLResponse)
async def account_mfa_page(
    request: Request, auth: AuthService = Depends(get_auth), db: Session = Depends(get_session)
):
    """Link a new authenticator; only shortly after password plus code on /account."""
    user, session = current_session(request, auth, db)
    if user is None or session is None or not session.complete:
        return RedirectResponse("/login?next=/account", status_code=303)
    if not _reauth_ok(request, auth, user):
        return RedirectResponse("/account", status_code=303)
    return _new_candidate(request, auth, user, "/account", reenrol=True)


@router.post("/account/mfa", response_class=HTMLResponse)
async def account_mfa_submit(
    request: Request,
    code: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    user, session = current_session(request, auth, db)
    if user is None or session is None or not session.complete:
        return RedirectResponse("/login?next=/account", status_code=303)
    if not _reauth_ok(request, auth, user):
        return RedirectResponse("/account", status_code=303)
    secret = auth.setup_secret(user, _read_cookie(request, SETUP_SECRET_COOKIE))
    if secret is None:
        return RedirectResponse("/account/mfa", status_code=303)
    codes = auth.enrol_totp(db, secret, code)
    if codes is None:
        return _render_setup(
            request,
            user,
            secret,
            "/account",
            True,
            status=400,
            error="De code klopt niet. Controleer de tijd op je telefoon.",
        )
    log.warning("new authenticator linked for %s; other sessions logged out", user.username)
    response = _page(request, "auth/recovery_codes.html", codes=codes, next="/account", fresh=True)
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _clear_cookie(response, request, REAUTH_COOKIE)
    _reissue_session(response, request, auth, user, session)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Logs out this browser; "Overal uitloggen" on /account ends every session."""
    response = RedirectResponse("/login", status_code=303)
    for name in (COOKIE_NAME, SETUP_SECRET_COOKIE, REAUTH_COOKIE):
        _clear_cookie(response, request, name)
    return response
