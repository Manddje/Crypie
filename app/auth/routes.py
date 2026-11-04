"""Setup and login pages, second-factor enrolment, and the middleware that protects the rest."""

from __future__ import annotations

import functools
import ipaddress
import logging
import math
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import segno
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import totp
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.service import (
    KNOWN_DEVICE_SECONDS,
    MFA_SETUP_SECONDS,
    REAUTH_SECONDS,
    RECOVERY_CODE_COUNT,
    SETUP_RESUME_SECONDS,
    STAGE_ONE_SECONDS,
    AuthService,
    LoginThrottle,
    validate_new_password,
)
from app.auth.session import COOKIE_NAME, LOGIN, REAUTH, SESSION, SessionData, cookie_name
from app.db import AuthUser, SessionLocal, get_session
from app.flash import add_flash, redirect_with_flash
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
# Set in the browser that made the account at /setup; it survives 'Later koppelen' (a logout).
SETUP_RESUME_COOKIE = "crypie_setup_resume"
# Set after a complete login; it survives logging out. Such a browser is never held up by the
# account-wide password limit (see AuthService.password_throttle).
KNOWN_DEVICE_COOKIE = "crypie_device"

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


class LoginDisabled(Exception):
    """A login page was opened while the login is off (AUTH_ENABLED=false); the app turns
    this into a redirect to the overview with a short message (see app/errors.py)."""


def get_auth(request: Request) -> AuthService:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        raise LoginDisabled
    return auth


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


def _password_account_key(user: AuthUser) -> str:
    """Throttle key for wrong passwords to the account from browsers that never logged in."""
    return f"pw:{user.account_id}"


def _known_device(request: Request, auth: AuthService, user: AuthUser | None) -> bool:
    return user is not None and auth.known_device(user, _read_cookie(request, KNOWN_DEVICE_COOKIE))


def _remember_device(response: Response, request: Request, auth: AuthService, user: AuthUser):
    _set_cookie(
        response, request, KNOWN_DEVICE_COOKIE, auth.known_device_token(user), KNOWN_DEVICE_SECONDS
    )


def _wait_text(wait: int) -> str:
    """'29 seconden', '1 minuut', '15 minuten'."""
    wait = max(int(wait), 1)
    if wait < 60:
        return f"{wait} seconden"
    minutes = math.ceil(wait / 60)
    return "1 minuut" if minutes == 1 else f"{minutes} minuten"


def _wait_message(wait: int) -> str:
    until = datetime.now(UTC) + timedelta(seconds=max(int(wait), 1))
    return (
        f"Te veel mislukte pogingen. Probeer het over {_wait_text(wait)} opnieuw "
        f"(om {until:%H:%M} UTC)."
    )


def _attempts_note(
    auth: AuthService, db: Session, key: str, throttle: LoginThrottle | None = None
) -> str:
    """After a wrong password or code: the wait that starts now, or a warning when only one or
    two attempts are left before it does."""
    throttle = throttle or auth.throttle
    wait = throttle.blocked_for(db, key)
    if wait:
        return " " + _wait_message(wait)
    left = throttle.max_failures - throttle.failures(db, key)
    if 0 < left <= 2:
        word = "poging" if left == 1 else "pogingen"
        return f" Nog {left} {word}; daarna moet je even wachten."
    return ""


# Why the login page is shown again (a flash on /login).
EXPIRED_GET = (
    "Je bent uitgelogd: je sessie is verlopen of elders beëindigd. Log opnieuw in; daarna ga je "
    "verder waar je was."
)
EXPIRED_POST = (
    "Je sessie is verlopen; je wijziging is niet opgeslagen. Log opnieuw in; daarna kom je "
    "terug op de pagina en kun je het opnieuw proberen."
)
STAGE_EXPIRED = (
    "Het inloggen duurde te lang: tussen wachtwoord en code mogen maximaal "
    f"{STAGE_ONE_SECONDS // 60} minuten zitten. Log opnieuw in."
)
LOGGED_OUT = "Je bent uitgelogd."
LOCKED_OUT_HELP = (
    "Weet je het zeker en wil je niet wachten? Op de server heft python -m app.auth "
    "clear-throttle de blokkade op."
)
LOGGED_OUT_EVERYWHERE = "Je bent overal uitgelogd: elke browser moet opnieuw inloggen."
ACCOUNT_LOCKED_HELP = (
    "Er zijn vanaf veel adressen foute wachtwoorden geprobeerd voor dit account. Een browser "
    "waarin je eerder volledig hebt ingelogd heeft hier geen last van; op de server heft python "
    "-m app.auth clear-throttle de blokkade op."
)


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", request.app.state.settings)
    ctx.setdefault("error", None)
    ctx.setdefault("stage_minutes", STAGE_ONE_SECONDS // 60)
    ctx.setdefault("resume_minutes", setup_resume_minutes())
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
    response = RedirectResponse(
        f"/login?next={_next_param(target)}" if target != "/" else "/login", status_code=303
    )
    if _read_cookie(request, COOKIE_NAME) and session is None:
        # There was a login, but it expired or was ended elsewhere: say so on /login, and that
        # a form that was being sent did not go through.
        add_flash(response, "warn", EXPIRED_POST if request.method == "POST" else EXPIRED_GET)
    return response


def _next_param(target: str) -> str:
    """``target`` for a ?next= parameter: its own '&' and '#' must not end the parameter."""
    return quote(target, safe="/?=")


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
        return _page(
            request,
            "auth/setup_token.html",
            status=429,
            error=_wait_message(wait),
            hide_flashes=True,
        )
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
            hide_flashes=True,
        )
    return _page(request, "auth/setup_token.html", status=404, hide_flashes=True)


def enrolment_unfinished(user: AuthUser | None, now: datetime | None = None) -> bool:
    """An account made at /setup whose first authenticator was never linked: no authenticator,
    never logged in, no one-time enrol code from the server, and made less than
    SETUP_RESUME_SECONDS ago. Only then, and only in the browser that made the account (see
    may_resume_enrolment), may the setup link plus the password start the linking again (after
    'Later koppelen' or the time limit). Later, elsewhere, or once ``reset-mfa`` or
    ``create-account`` issued an enrol code, only that code works: it needs a shell on the
    server, where read access to the logs alone is not enough."""
    if (
        user is None
        or user.totp_secret
        or user.last_login_at is not None
        or user.enrol_token_hash
        or user.recovery_codes
    ):
        return False
    created = user.created_at
    if created is None:
        return False
    if created.tzinfo is None:  # SQLite hands back naive datetimes
        created = created.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return timedelta(0) <= now - created < timedelta(seconds=SETUP_RESUME_SECONDS)


def setup_resume_minutes() -> int:
    return SETUP_RESUME_SECONDS // 60


def may_resume_enrolment(request: Request, auth: AuthService, user: AuthUser | None) -> bool:
    """enrolment_unfinished, in the browser that made the account: whoever only has the setup
    token (it is in the server log) and the password cannot link an authenticator of their own."""
    return (
        user is not None
        and enrolment_unfinished(user)
        and auth.setup_resume_allowed(user, _read_cookie(request, SETUP_RESUME_COOKIE))
    )


RESUME_CLOSED = (
    "Opnieuw beginnen met de setup-link kan alleen in de browser waarin je het account aanmaakte, "
    f"en tot {SETUP_RESUME_SECONDS // 60} minuten daarna. Log in met je wachtwoord en vul de code "
    "in die de server geeft met python -m app.auth reset-mfa."
)


def _setup_closed(user: AuthUser | None) -> RedirectResponse:
    """The setup link no longer opens anything for this account: off to /login, with the way out
    when no authenticator is linked yet."""
    if user is not None and not user.mfa_enrolled:
        return redirect_with_flash("/login", "info", RESUME_CLOSED)
    return RedirectResponse("/login", status_code=303)


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
        user = auth.account(db)
        if may_resume_enrolment(request, auth, user):
            return _page(request, "auth/setup_resume.html", username="", token=token)
        return _setup_closed(user)
    return _page(request, "auth/setup.html", username="", token=token)


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
    token: str = Form(""),
    action: str = Form(""),
    auth: AuthService = Depends(get_auth),
    db: Session = Depends(get_session),
):
    if (gate := _setup_gate(request, auth, db, token)) is not None:
        return gate
    if auth.has_account(db):
        user = auth.account(db)
        if action == "resume" and may_resume_enrolment(request, auth, user):
            return await _resume_enrolment(request, auth, db, user, username, password, token)
        return _setup_closed(user)
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
    # And this browser may start the linking again with the setup link for a while.
    _set_cookie(
        response,
        request,
        SETUP_RESUME_COOKIE,
        auth.setup_resume_token(user),
        SETUP_RESUME_SECONDS,
    )
    return response


async def _resume_enrolment(
    request: Request,
    auth: AuthService,
    db: Session,
    user: AuthUser,
    username: str,
    password: str,
    token: str,
) -> Response:
    """Setup link plus password: link the first authenticator after all (see
    enrolment_unfinished). Wrong passwords count like a wrong login."""
    key = _client_key(request)

    def again(status: int, error: str) -> HTMLResponse:
        return _page(
            request,
            "auth/setup_resume.html",
            status=status,
            username=username[:64],
            token=token,
            error=error,
        )

    try:
        ok = await _hashing(_verify, password, user.password_hash)
    except _Busy:
        return again(429, BUSY)
    if not ok or not auth.username_matches(user, username):
        auth.throttle.record_failure(db, key)
        log.warning("wrong password to resume enrolment from %s", key)
        return again(
            401, "Gebruikersnaam of wachtwoord klopt niet." + _attempts_note(auth, db, key)
        )
    auth.throttle.reset(db, key)
    log.warning("first enrolment of %s restarted with the setup link from %s", user.username, key)
    response = RedirectResponse("/login/mfa/setup", status_code=303)
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
        return again(429, _wait_message(wait) + " " + LOCKED_OUT_HELP)
    user = auth.account(db)
    # Browsers that never logged in also share one account-wide limit, so spreading the guesses
    # over many addresses does not help; a known browser (the owner's) is never held up by it.
    account_key = None
    if user is not None and not _known_device(request, auth, user):
        account_key = _password_account_key(user)
        wait = auth.password_throttle.blocked_for(db, account_key)
        if wait:
            return again(429, _wait_message(wait) + " " + ACCOUNT_LOCKED_HELP)
    try:
        ok = await _hashing(_verify, password, user.password_hash if user else None)
    except _Busy:
        return again(429, BUSY)
    if user is None or not ok or not auth.username_matches(user, username):
        auth.throttle.record_failure(db, key)
        note = _attempts_note(auth, db, key)
        if account_key is not None:
            auth.password_throttle.record_failure(db, account_key)
            wait = auth.password_throttle.blocked_for(db, account_key)
            if wait:
                log.warning("wrong passwords from many addresses: account-wide wait started")
                note = " " + _wait_message(max(wait, auth.throttle.blocked_for(db, key)))
                note += " " + ACCOUNT_LOCKED_HELP
        log.warning("failed login from %s", key)
        return again(401, "Gebruikersnaam of wachtwoord klopt niet." + note)
    if needs_rehash(user.password_hash):
        try:
            auth.store_rehash(db, user, await _hashing(hash_password, password))
            log.info("password hash upgraded for %s", user.username)
        except _Busy:
            pass  # next time
    target = _safe_next(next)
    where = "/login/mfa" if user.mfa_enrolled else "/login/mfa/setup"
    response = RedirectResponse(f"{where}?next={_next_param(target)}", status_code=303)
    _set_cookie(response, request, COOKIE_NAME, auth.stage_one_token(user), STAGE_ONE_SECONDS)
    return response


# -- step 2: authenticator code ------------------------------------------------------------


def _back_to_login(request: Request, next: str) -> RedirectResponse:
    """The second step without a valid password step: back to /login, saying why when the
    password step had simply expired."""
    response = RedirectResponse(f"/login?next={_next_param(_safe_next(next))}", status_code=303)
    if _read_cookie(request, COOKIE_NAME):
        add_flash(response, "warn", STAGE_EXPIRED)
    return response


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
        return _back_to_login(request, next)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if not user.mfa_enrolled:
        return RedirectResponse(
            f"/login/mfa/setup?next={_next_param(_safe_next(next))}", status_code=303
        )
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
        return _back_to_login(request, next)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    wait = _second_factor_wait(request, auth, db, user)
    # A code with letters is a recovery code: show that field again after an error.
    mode = "recovery" if any(c.isalpha() for c in code) else "app"
    if wait:
        return _page(
            request,
            "auth/mfa.html",
            status=429,
            next=_safe_next(next),
            error=_wait_message(wait) + " " + LOCKED_OUT_HELP,
            mode=mode,
        )
    kind = auth.check_second_factor(db, code)
    if kind is None:
        _second_factor_failed(request, auth, db, user)
        error = (
            "Deze herstelcode klopt niet of is al gebruikt."
            if mode == "recovery"
            else "De code klopt niet. Controleer de tijd op je telefoon en probeer het opnieuw."
        )
        note = _attempts_note(auth, db, _account_key(user)) or _attempts_note(
            auth, db, _client_key(request)
        )
        return _page(
            request,
            "auth/mfa.html",
            status=401,
            next=_safe_next(next),
            error=error + note,
            mode=mode,
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
    _remember_device(response, request, auth, user)
    if kind == "recovery":
        left = auth.recovery_codes_left(db)
        add_flash(
            response,
            "warn" if left <= 2 else "info",
            f"Je bent ingelogd met een herstelcode. Je hebt er nog {left} van "
            f"{RECOVERY_CODE_COUNT}. Ben je je authenticator kwijt? Stel dan via Account een "
            "nieuwe in; daar maak je ook nieuwe herstelcodes.",
        )
    return response


# -- enrolment ---------------------------------------------------------------------------


def _render_setup(
    request: Request,
    user: AuthUser,
    secret: str,
    next: str,
    reenrol: bool,
    status: int = 200,
    deadline: int | None = None,
    **ctx,
) -> HTMLResponse:
    """``deadline`` (epoch seconds) is when the right to link ends: the password step of a
    first enrolment, or the re-check on /account."""
    uri = totp.provisioning_uri(secret, user.username, issuer="Crypie")
    qr = segno.make(uri, error="m").svg_inline(scale=5, border=1, dark="#000", light=None)
    left = max(int((deadline or 0) - time.time()), 0) if deadline else None
    return _page(
        request,
        "auth/mfa_setup.html",
        status=status,
        qr_svg=qr,
        secret_pretty=totp.pretty_secret(secret),
        next=next,
        reenrol=reenrol,
        action="/account/mfa" if reenrol else "/login/mfa/setup",
        deadline=datetime.fromtimestamp(deadline, UTC) if deadline else None,
        minutes_left=math.ceil(left / 60) if left else None,
        **ctx,
    )


def _new_candidate(
    request: Request,
    auth: AuthService,
    user: AuthUser,
    next: str,
    reenrol: bool,
    deadline: int | None = None,
) -> HTMLResponse:
    secret = totp.new_secret()
    response = _render_setup(request, user, secret, next, reenrol, deadline=deadline)
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
        return _back_to_login(request, next)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if user.mfa_enrolled:
        return RedirectResponse(f"/login/mfa?next={_next_param(_safe_next(next))}", status_code=303)
    if not session.extra.get("enrol"):
        return _page(request, "auth/enrol_token.html", next=_safe_next(next))
    return _new_candidate(
        request, auth, user, _safe_next(next), reenrol=False, deadline=session.expires_at
    )


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
        return _back_to_login(request, next)
    if session.complete:
        return RedirectResponse(_safe_next(next), status_code=303)
    if user.mfa_enrolled:
        return RedirectResponse(f"/login/mfa?next={_next_param(_safe_next(next))}", status_code=303)
    if not session.extra.get("enrol"):
        return _check_enrol_token(request, auth, db, user, enrol_token, _safe_next(next))
    secret = auth.setup_secret(user, _read_cookie(request, SETUP_SECRET_COOKIE))
    if secret is None:
        return RedirectResponse(
            f"/login/mfa/setup?next={_next_param(_safe_next(next))}", status_code=303
        )
    codes = auth.enrol_totp(db, secret, code)
    if codes is None:
        return _render_setup(
            request,
            user,
            secret,
            _safe_next(next),
            False,
            status=400,
            deadline=session.expires_at,
            error="De code klopt niet. Controleer de tijd op je telefoon.",
        )
    auth.throttle.reset(db, _client_key(request))
    log.info("authenticator enrolled for %s", user.username)
    response = _page(
        request,
        "auth/recovery_codes.html",
        codes=codes,
        next=_safe_next(next),
        fresh=True,
        first=True,
        username=user.username,
    )
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _clear_cookie(response, request, SETUP_RESUME_COOKIE)
    _set_cookie(response, request, COOKIE_NAME, auth.full_token(user), auth.session_seconds)
    _remember_device(response, request, auth, user)
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
    response = RedirectResponse(f"/login/mfa/setup?next={_next_param(next)}", status_code=303)
    _set_cookie(
        response, request, COOKIE_NAME, auth.stage_one_token(user, enrol=True), STAGE_ONE_SECONDS
    )
    return response


# -- account -----------------------------------------------------------------------------


def _account_page(
    request: Request, auth: AuthService, db: Session, status: int = 200, **ctx
) -> HTMLResponse:
    ctx.setdefault("message", None)
    ctx.setdefault("form", None)
    _, session = current_session(request, auth, db)
    return _page(
        request,
        "auth/account.html",
        status=status,
        user=auth.user(db),
        codes_left=auth.recovery_codes_left(db),
        codes_total=RECOVERY_CODE_COUNT,
        session_expires=datetime.fromtimestamp(session.expires_at, UTC) if session else None,
        session_hours=auth.session_seconds // 3600,
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
        response = redirect_with_flash("/login", "info", LOGGED_OUT_EVERYWHERE)
        for name in (COOKIE_NAME, SETUP_SECRET_COOKIE, REAUTH_COOKIE):
            _clear_cookie(response, request, name)
        return response
    key = _client_key(request)
    form = action if action in ("recovery", "reenrol", "password") else None

    def failed(status: int, error: str, field: str = "password") -> HTMLResponse:
        # The error belongs to the form that was sent: shown in its card, its field marked.
        return _account_page(
            request, auth, db, status=status, error=error, form=form, bad_field=field
        )

    wait = auth.throttle.blocked_for(db, key)
    if wait:
        return failed(
            429,
            _wait_message(wait) + " Zolang de blokkade duurt, kun je ook niet opnieuw inloggen.",
        )
    try:
        ok = await _hashing(verify_password, password, user.password_hash)
    except _Busy:
        return failed(429, BUSY)
    if not ok:
        auth.throttle.record_failure(db, key)
        log.warning("wrong password on /account from %s", key)
        return failed(401, "Het wachtwoord klopt niet." + _attempts_note(auth, db, key))
    auth.throttle.reset(db, key)
    if action in ("recovery", "reenrol"):
        # New recovery codes are a lasting second factor, like a new authenticator: both
        # need a current code too, so a stolen cookie plus the password is not enough.
        wait = _second_factor_wait(request, auth, db, user)
        if wait:
            return failed(429, _wait_message(wait), "code")
        kind = auth.check_second_factor(db, code)
        if kind is None:
            _second_factor_failed(request, auth, db, user)
            return failed(
                401,
                "De code klopt niet. Gebruik een code uit je authenticator-app of een herstelcode.",
                "code",
            )
        auth.throttle.reset(db, _account_key(user))
        if kind == "recovery":
            log.warning("recovery code used for %s from %s", user.username, key)
    if action == "recovery":
        codes = auth.issue_recovery_codes(db)
        log.info("new recovery codes issued for %s", user.username)
        response = _page(
            request,
            "auth/recovery_codes.html",
            codes=codes,
            next="/account",
            fresh=False,
            username=user.username,
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
            return failed(400, error, "new_password")
        try:
            hashed = await _hashing(hash_password, new_password)
        except _Busy:
            return failed(429, BUSY)
        auth.change_password(db, password_hash=hashed)
        log.warning("password changed for %s; other sessions logged out", user.username)
        response = _account_page(
            request,
            auth,
            db,
            message="Je wachtwoord is gewijzigd. Andere browsers zijn uitgelogd.",
            form="password",
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


def step_up_attempts_left(request: Request, db: Session) -> int | None:
    """How many wrong passwords this address may still try before the wait starts (the same
    count as the login), or None without the login."""
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None:
        return None
    used = auth.throttle.failures(db, _client_key(request))
    return max(auth.throttle.max_failures - used, 0)


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
    response = _page(
        request,
        "auth/recovery_codes.html",
        codes=codes,
        next="/account",
        fresh=True,
        username=user.username,
    )
    _clear_cookie(response, request, SETUP_SECRET_COOKIE)
    _clear_cookie(response, request, REAUTH_COOKIE)
    _reissue_session(response, request, auth, user, session)
    return response


@router.post("/logout")
async def logout(request: Request):
    """Logs out this browser; "Overal uitloggen" on /account ends every session."""
    if getattr(request.app.state, "auth", None) is None:
        response = RedirectResponse("/login", status_code=303)
    else:
        response = redirect_with_flash("/login", "info", LOGGED_OUT)
    for name in (COOKIE_NAME, SETUP_SECRET_COOKIE, REAUTH_COOKIE):
        _clear_cookie(response, request, name)
    return response
