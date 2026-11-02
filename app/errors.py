"""Central error handling: HTML error pages in the normal layout, JSON for the API.

- Paths under /api/ (and clients that do not ask for HTML) keep FastAPI's JSON errors.
- A GET on a form address that only takes POST (/orders, /trading/action, /news/refresh,
  /journal/1/note, ...) is sent on to its page with a 303 instead of a bare 405.
- A refused form POST (a 4xx raised by the handler, invalid input, Bitvavo down) goes back to
  its page with a one-time message (Post/Redirect/Get), so reloading never resends it.
- Anything else becomes ``error.html`` with a way back; a crash (500) too, via
  :class:`ServerErrorPages`, which sits inside the security headers so the page keeps them.
"""

from __future__ import annotations

import html
import logging
import re

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.routes import LoginDisabled
from app.bitvavo.client import BitvavoError
from app.flash import redirect_with_flash
from app.trading.health import describe_problem
from app.web import templates

log = logging.getLogger("crypie")

PAGE_NAMES = {
    "/": "Overzicht",
    "/trading": "Handelen",
    "/journal": "Journaal",
    "/plan": "Tradingplan",
    "/strategy": "Strategie",
    "/news": "Nieuws",
    "/settings": "Instellingen",
    "/account": "Account",
}
LOGIN_OFF_MESSAGE = (
    "Inloggen staat uit op deze server. Daarom zijn er geen inlogpagina en geen account; "
    "je bent naar het overzicht gestuurd."
)
# Dutch names for form fields in "invalid input" messages.
FIELD_NL = {
    "amount_quote": "bedrag",
    "market": "markt",
    "side": "kopen of verkopen",
    "action": "actie",
    "limit": "aantal candles",
    "note": "notitie",
    "tags": "labels",
    "back": "terugadres",
    "hours": "uren",
    "days": "dagen",
    "telt": "filter",
    "position_id": "het nummer van de trade",
    "evaluation_id": "het nummer van de evaluatie",
    "page": "het paginanummer",
    "period": "de periode",
    "interval": "het interval",
    "view": "de weergave",
    "mode": "de modus",
    "base_version": "de versie van de strategie",
    "strategy_json": "de strategie",
    "description": "de omschrijving",
    "order_token": "het formulier",
    "sell_all": "hele positie verkopen",
    "current_password": "het wachtwoord",
    "result": "het resultaat",
    "saved": "de melding",
    "next": "het terugadres",
    "next_url": "het terugadres",
}
_JOURNAL_NOTE_RE = re.compile(r"^/journal/([0-9]{1,10})/note/?$")


def is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.url.path == "/openapi.json"


def wants_html(request: Request) -> bool:
    """A browser page request: not the API, and HTML is acceptable to the client."""
    return not is_api(request) and "text/html" in request.headers.get("accept", "")


def _has_get_route(app: FastAPI, path: str) -> bool:
    scope = {"type": "http", "path": path, "root_path": "", "method": "GET"}
    for route in app.router.routes:
        if isinstance(route, Mount):
            continue
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return True
    return False


def page_for(app: FastAPI, path: str) -> str:
    """The page a form address belongs to: the nearest parent path with a GET page.

    /trading/action -> /trading, /news/refresh -> /news, /orders -> /, and a journal note
    goes back to its trade (/journal#trade-1).
    """
    if m := _JOURNAL_NOTE_RE.match(path):
        return f"/journal#trade-{m.group(1)}"
    parts = [p for p in path.split("/") if p]
    for n in range(len(parts), 0, -1):
        candidate = "/" + "/".join(parts[:n])
        if candidate == "/api" or candidate.startswith("/api/"):
            continue
        if _has_get_route(app, candidate):
            return candidate
    return "/"


def _back(app: FastAPI, path: str) -> tuple[str, str]:
    target = page_for(app, path)
    base = target.split("#", 1)[0]
    name = PAGE_NAMES.get(base)
    return target, (f"Terug naar {name}" if name else "Terug")


def _sentence(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


DEFAULT_DETAILS = {
    400: "Het verzoek klopt niet.",
    401: "Je bent niet ingelogd.",
    403: "Dit mag niet.",
    404: "Deze pagina bestaat niet (meer).",
    405: "Dit adres kun je niet zo openen.",
    409: "Dat kan nu even niet.",
    413: "Wat je verstuurde is te groot.",
    429: "Te veel verzoeken. Wacht even en probeer het opnieuw.",
}
TITLES = {
    400: "Dat lukte niet",
    401: "Niet ingelogd",
    403: "Geen toegang",
    404: "Pagina niet gevonden",
    405: "Dit is geen pagina",
    409: "Even geduld",
    429: "Te veel verzoeken",
    500: "Er ging iets mis",
    502: "Bitvavo is niet bereikbaar",
}


def _detail_text(exc: StarletteHTTPException) -> str:
    detail = exc.detail
    standard = isinstance(detail, str) and detail.strip() in (
        "Not Found",
        "Method Not Allowed",
        "Bad Request",
        "Forbidden",
        "Unauthorized",
    )
    if isinstance(detail, str) and detail.strip() and not standard:
        return _sentence(detail)
    if isinstance(detail, dict) and detail:
        return _sentence("; ".join(f"{k}: {v}" for k, v in detail.items()))
    return DEFAULT_DETAILS.get(exc.status_code, "Dat lukte niet.")


def bitvavo_text(exc: BitvavoError) -> str:
    """What went wrong at Bitvavo, in Dutch; a refusal keeps Bitvavo's own reason."""
    if 0 < exc.status < 500 and not exc.rate_limited:
        return _sentence(f"Bitvavo weigerde het verzoek ({exc})")
    return _sentence(describe_problem(exc))


def _validation_lines(exc: RequestValidationError) -> list[str]:
    lines = []
    for err in exc.errors()[:6]:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        # Never the internal parameter name in the Dutch text (an unknown one is 'een waarde').
        name = FIELD_NL.get(loc[-1], "een waarde") if loc else "invoer"
        kind = str(err.get("type", ""))
        if kind == "missing":
            what = "ontbreekt"
        elif kind.endswith("_parsing") or kind in ("float_type", "int_type"):
            what = "is geen geldig getal" if "float" in kind or "int" in kind else "is ongeldig"
        elif kind.startswith(("string_too_long", "too_long")):
            what = "is te lang"
        elif kind.startswith(("greater_than", "less_than")):
            what = "valt buiten het toegestane bereik"
        elif kind.startswith("string_pattern"):
            what = "heeft een ongeldige vorm"
        else:
            what = "is ongeldig"
        lines.append(f"{name} {what}")
    return lines


def error_page(
    request: Request,
    status_code: int,
    message: str,
    *,
    title: str | None = None,
    kind: str = "error",
    details: list[str] | None = None,
    back: tuple[str, str] | None = None,
    logout: bool = False,
    show_code: bool = True,
) -> Response:
    """``error.html`` in the normal layout (the login layout for visitors without a login)."""
    app = request.app
    try:
        state = app.state
        settings = getattr(state, "settings", None)
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        auth_on = getattr(state, "auth", None) is not None
        user = getattr(request.state, "user", None)
        if back is None:
            back = _back(app, request.url.path)
        ctx = {
            "settings": settings,
            "title": title or TITLES.get(status_code, "Dat lukte niet"),
            "message": message,
            "kind": kind,
            "details": details or [],
            "status_code": status_code if show_code else None,
            "back_href": back[0],
            "back_label": back[1],
            "logout": logout,
            "anonymous": auth_on and not user,
        }
        return templates.TemplateResponse(request, "error.html", ctx, status_code=status_code)
    except Exception:  # noqa: BLE001 - an error page must never fail itself
        log.exception("rendering the error page failed")
        body = (
            '<!doctype html><html lang="nl"><meta charset="utf-8"><title>Crypie</title>'
            f"<h1>{html.escape(title or 'Er ging iets mis')}</h1><p>{html.escape(message)}</p>"
            '<p><a href="/">Naar overzicht</a></p></html>'
        )
        return HTMLResponse(body, status_code=status_code)


def _logout_page(request: Request) -> Response:
    """GET /logout (typed in or a bookmark): a small page with the logout button."""
    if getattr(request.app.state, "auth", None) is None:
        return redirect_with_flash("/", "info", LOGIN_OFF_MESSAGE)
    if not getattr(request.state, "user", None):
        return RedirectResponse("/login", status_code=303)
    return error_page(
        request,
        200,
        "Wil je uitloggen in deze browser? Andere browsers blijven ingelogd; die log je uit "
        "via Account.",
        title="Uitloggen",
        kind="info",
        logout=True,
        back=("/", "Naar overzicht"),
        show_code=False,
    )


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if is_api(request):
        return await http_exception_handler(request, exc)
    path, method = request.url.path, request.method
    if exc.status_code == 405 and method in ("GET", "HEAD"):
        # A form address opened as a page (bookmark, reload after a redirect, typed in).
        if path.rstrip("/") == "/logout":
            return _logout_page(request)
        return RedirectResponse(page_for(request.app, path), status_code=303)
    if not wants_html(request):
        return await http_exception_handler(request, exc)
    text = _detail_text(exc)
    # Raised by one of our handlers (not: no route, or a missing file under /static).
    routed = request.scope.get("endpoint") is not None and not isinstance(
        request.scope.get("route"), Mount
    )
    if method not in ("GET", "HEAD") and routed and 400 <= exc.status_code < 500:
        return redirect_with_flash(page_for(request.app, path), "error", text)
    if exc.status_code == 404 and not routed:
        text = "Deze pagina bestaat niet. Controleer het adres, of ga terug naar het overzicht."
        return error_page(request, 404, text, back=("/", "Naar overzicht"))
    return error_page(request, exc.status_code, text)


async def validation_error(request: Request, exc: RequestValidationError) -> Response:
    if not wants_html(request):
        return await request_validation_exception_handler(request, exc)
    lines = _validation_lines(exc)
    text = "Niet verwerkt: " + "; ".join(lines) + "." if lines else "Niet verwerkt."
    if request.method not in ("GET", "HEAD"):
        return redirect_with_flash(page_for(request.app, request.url.path), "error", text)
    page = request.url.path
    return error_page(
        request,
        400,
        "Het adres bevat een ongeldige waarde: " + "; ".join(lines) + ".",
        title="Ongeldig adres",
        back=(page, f"Naar {PAGE_NAMES.get(page, 'de pagina')} zonder filters"),
    )


async def bitvavo_error(request: Request, exc: BitvavoError) -> Response:
    log.warning("%s %s: %s", request.method, request.url.path, exc)
    if not wants_html(request):
        return JSONResponse({"detail": str(exc)}, status_code=502)
    text = bitvavo_text(exc)
    path = request.url.path
    if request.method not in ("GET", "HEAD"):
        if path == "/trading/action":
            text = (
                f"Cyclus niet gedraaid: {text} De automatische cyclus probeert het later opnieuw."
            )
        else:
            text = f"Niet gelukt: {text} Probeer het later opnieuw."
        return redirect_with_flash(page_for(request.app, path), "error", text)
    return error_page(request, 502, text + " Probeer het over een paar minuten opnieuw.")


async def login_disabled(request: Request, exc: LoginDisabled) -> Response:
    """Login pages (/login, /setup, /account, ...) while AUTH_ENABLED=false."""
    return redirect_with_flash("/", "info", LOGIN_OFF_MESSAGE)


def server_error_response(request: Request) -> Response:
    if not wants_html(request):
        return JSONResponse({"detail": "Interne fout"}, status_code=500)
    # This response is made outside FlashMiddleware, so it cannot clear the flash cookie:
    # leave pending messages for the next page instead of showing them here and again there
    # (a green 'gelukt' next to 'mogelijk niet uitgevoerd' would contradict itself too).
    request.state.flashes = []
    return error_page(
        request,
        500,
        "Er ging iets mis op de server. Wat je deed is mogelijk niet uitgevoerd: controleer het "
        "op de pagina voordat je het opnieuw probeert. De details staan in de serverlog.",
    )


class ServerErrorPages:
    """A crash in a handler becomes the 500 page (or JSON for the API) instead of a bare
    'Internal Server Error'. Added inside SecurityHeaders, so the page keeps the CSP and the
    other headers; the exception is raised again afterwards, so the server still logs it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            if started:
                raise
            response = server_error_response(Request(scope))
            await response(scope, receive, send)
            raise


def install(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(BitvavoError, bitvavo_error)
    app.add_exception_handler(LoginDisabled, login_disabled)
