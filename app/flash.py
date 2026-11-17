"""One-time messages ("flashes") shown on the next page after an action.

A handler that redirects after a POST (Post/Redirect/Get) attaches a message with
:func:`redirect_with_flash` or :func:`add_flash`. :class:`FlashMiddleware` turns it into a
short-lived, HMAC-signed cookie; on the next request it puts the messages in
``request.state.flashes`` (a list of ``{"kind": ..., "text": ...}``) and clears the cookie as soon
as an HTML page has been rendered with them. Redirects, JSON and static files leave the cookie
alone, so a message survives the redirect chain (also through /login) until a page shows it.

The text is plain text: templates escape it. Never put secrets in a flash.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import secrets
import time
from hashlib import sha256
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE_NAME = "crypie_flash"
HOST_PREFIX = "__Host-"
# A flash is meant for the very next page; whatever is not shown within this time is dropped.
MAX_AGE_S = 120
KINDS = ("ok", "error", "warn", "info")
MAX_FLASHES = 4
MAX_TEXT = 400
# Browsers keep cookies up to about 4 KB; stay well below.
MAX_COOKIE_BYTES = 3500
# Internal response header from add_flash() to the middleware; never sent to the browser.
PENDING_HEADER = "x-crypie-flash"
SKIP_PREFIXES = ("/static/",)
# The section a flash belongs to: the #fragment of the redirect. The page shows it there (where
# the browser lands) instead of at the top of the page, out of view (UX-99).
_ANCHOR = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,40}")

_key = secrets.token_bytes(32)


def configure(secret: str | None) -> None:
    """Sign with a key derived from ``secret`` (SESSION_SECRET or the stored session secret), so
    flashes survive a restart and work across instances; without one a random key per process
    is used (a restart then drops pending flashes, which is harmless)."""
    global _key
    if secret:
        _key = hmac.new(secret.encode(), b"crypie-flash-v1", sha256).digest()
    else:
        _key = secrets.token_bytes(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _clean(flashes: Any) -> list[dict[str, str]]:
    """Valid flashes only: known kind, non-empty text, bounded length and count."""
    out: list[dict[str, str]] = []
    if not isinstance(flashes, list):
        return out
    for f in flashes:
        if not isinstance(f, dict):
            continue
        kind, text = f.get("kind"), f.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        item = {"kind": kind if kind in KINDS else "info", "text": text.strip()[:MAX_TEXT]}
        at = f.get("at")
        if isinstance(at, str) and _ANCHOR.fullmatch(at):
            item["at"] = at
        out.append(item)
    # The newest messages matter most when there are too many.
    return _dedupe(out)[-MAX_FLASHES:]


def _dedupe(flashes: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for f in flashes:
        key = (f["kind"], f["text"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def encode(flashes: list[dict[str, str]], now: float | None = None) -> str:
    """The signed cookie value for ``flashes`` (dropping the oldest ones if it gets too big)."""
    flashes = _clean(flashes)
    expires = int((time.time() if now is None else now) + MAX_AGE_S)
    while True:
        payload = _b64(json.dumps({"f": flashes, "exp": expires}, ensure_ascii=False).encode())
        mac = _b64(hmac.new(_key, payload.encode(), sha256).digest())
        value = f"{payload}.{mac}"
        if len(value) <= MAX_COOKIE_BYTES or len(flashes) <= 1:
            return value
        flashes = flashes[1:]


def decode(value: str | None, now: float | None = None) -> list[dict[str, str]]:
    """The flashes in a cookie value; [] when it is missing, forged, garbled or expired."""
    if not value or "." not in value or len(value) > MAX_COOKIE_BYTES * 2:
        return []
    payload, _, mac = value.partition(".")
    expected = _b64(hmac.new(_key, payload.encode(), sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return []
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    exp = data.get("exp")
    if not isinstance(exp, int) or exp < (time.time() if now is None else now):
        return []
    return _clean(data.get("f"))


def _pending(headers: Headers | MutableHeaders) -> list[dict[str, str]]:
    raw = headers.get(PENDING_HEADER)
    if not raw:
        return []
    try:
        return _clean(json.loads(_unb64(raw)))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return []


def add_flash(response: Response, kind: str, text: str, at: str | None = None) -> Response:
    """Show ``text`` once on the next page the browser renders. Returns ``response``.

    ``kind`` is "ok", "error", "warn" or "info". Several calls add several messages. ``at`` is
    the id of the section the message is about; a page that has a place for it there
    (``ui.flashes_here``) shows it in that section, otherwise it is shown at the top.
    """
    item = {"kind": kind, "text": text}
    if at:
        item["at"] = at
    flashes = _pending(response.headers) + [item]
    value = _b64(json.dumps(_clean(flashes), ensure_ascii=False).encode())
    response.headers[PENDING_HEADER] = value
    return response


def redirect_with_flash(url: str, kind: str, text: str, status_code: int = 303) -> RedirectResponse:
    """A redirect (303 by default: Post/Redirect/Get) that shows ``text`` on the target page,
    in the section the URL's #fragment points to when that page has a place for it."""
    response = RedirectResponse(url, status_code=status_code)
    fragment = url.partition("#")[2]
    add_flash(response, kind, text, at=fragment if _ANCHOR.fullmatch(fragment) else None)
    return response


def take_flashes(request: Any, at: str) -> list[dict[str, str]]:
    """The flashes meant for section ``at``, removed from the ones the page shows at the top."""
    state = getattr(request, "state", None)
    flashes = list(getattr(state, "flashes", None) or [])
    mine = [f for f in flashes if f.get("at") == at]
    if mine:
        state.flashes = [f for f in flashes if f.get("at") != at]
    return mine


def cookie_name(scope: Scope) -> str:
    return HOST_PREFIX + COOKIE_NAME if scope.get("scheme") == "https" else COOKIE_NAME


def _set_cookie_header(scope: Scope, value: str | None) -> str:
    """A Set-Cookie value: the flashes, or (``value`` None) an expired cookie that clears it."""
    name = cookie_name(scope)
    secure = "; Secure" if scope.get("scheme") == "https" else ""
    if value is None:
        return f"{name}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax{secure}"
    return f"{name}={value}; Max-Age={MAX_AGE_S}; Path=/; HttpOnly; SameSite=Lax{secure}"


def _shown(method: str, status: int, headers: MutableHeaders) -> bool:
    """True when this response is a page that renders the flashes (and so consumes them)."""
    if method not in ("GET", "POST") or 300 <= status < 400:
        return False
    return headers.get("content-type", "").startswith("text/html")


class FlashMiddleware:
    """Reads the flash cookie into ``request.state.flashes`` and writes or clears it."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        path = scope.get("path", "")
        if path.startswith(SKIP_PREFIXES):
            state["flashes"] = []
            await self.app(scope, receive, send)
            return
        name = cookie_name(scope)
        had_cookie = False
        for key, _, value in _cookies(Headers(scope=scope)):
            if key == name:
                had_cookie = True
                incoming = decode(value)
                break
        else:
            incoming = []
        state["flashes"] = list(incoming)
        method = scope.get("method", "GET").upper()

        async def send_with_flashes(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                new = _pending(headers)
                if PENDING_HEADER in headers:
                    del headers[PENDING_HEADER]
                consumed = _shown(method, message["status"], headers)
                keep = [] if consumed else incoming
                if new:
                    headers.append("set-cookie", _set_cookie_header(scope, encode(keep + new)))
                elif had_cookie and (consumed or not incoming):
                    # Shown now, or a cookie that was forged, garbled or expired: clear it.
                    headers.append("set-cookie", _set_cookie_header(scope, None))
            await send(message)

        await self.app(scope, receive, send_with_flashes)


def _cookies(headers: Headers):
    """(name, "=", value) for every cookie in the request, without unquoting."""
    for header in headers.getlist("cookie"):
        for part in header.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep:
                yield key, sep, value


def flashes_from_response(response: Any, with_at: bool = False) -> list[dict[str, str]]:
    """The flashes a response sets for the next page (for tests): decodes its Set-Cookie.
    Kind and text only, unless ``with_at`` (then also the section each one belongs to)."""
    for header in response.headers.get_list("set-cookie"):
        first = header.split(";", 1)[0]
        key, _, value = first.partition("=")
        if key in (COOKIE_NAME, HOST_PREFIX + COOKIE_NAME):
            found = decode(value)
            if with_at:
                return found
            return [{"kind": f["kind"], "text": f["text"]} for f in found]
    return []
