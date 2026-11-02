"""ASGI middlewares that harden every response: cross-origin protection for state-changing
requests, a request body size limit, and security headers."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Request bodies: the pages reachable without a login get a small limit, the rest one
# that still fits every form and JSON body the app takes.
MAX_BODY_BYTES = 1024 * 1024
PUBLIC_MAX_BODY_BYTES = 16 * 1024
PUBLIC_BODY_PATHS = {"/login", "/login/mfa", "/setup", "/logout"}

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)
# The /docs and /redoc pages (off unless API_DOCS_ENABLED=true, see app/api_docs.py) load
# version-pinned scripts with SRI hashes from jsDelivr; no inline script.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://cdn.redoc.ly; "
    "font-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
DOCS_PATHS = ("/docs", "/redoc")
NO_STORE_PREFIXES = ("/login", "/setup", "/account", "/logout")
HSTS = "max-age=31536000"


def _own_origin(scope: Scope, headers: Headers) -> str | None:
    host = headers.get("host")
    if not host:
        return None
    return f"{scope.get('scheme', 'http')}://{host}".lower()


class CrossOriginProtection:
    """Refuse state-changing requests that a browser sent from another origin.

    Same rules as Go's ``net/http.CrossOriginProtection``: when the browser sends
    ``Sec-Fetch-Site`` only "same-origin" and "none" (typed in, bookmarks) pass; otherwise an
    ``Origin`` header must be this site itself (or one of ``trusted_origins``); requests with
    neither header do not come from a (modern) browser and pass.
    """

    def __init__(self, app: ASGIApp, trusted_origins: Iterable[str] | None = None):
        self.app = app
        self.trusted_origins = trusted_origins

    def _trusted(self, scope: Scope) -> set[str]:
        origins = self.trusted_origins
        if origins is None:
            app = scope.get("app")
            env = getattr(getattr(app, "state", None), "env", None)
            if env is None:
                from app.config import get_settings

                env = get_settings()
            origins = getattr(env, "trusted_origins", [])
        return {o.strip().rstrip("/").lower() for o in origins if o.strip()}

    def allowed(self, scope: Scope) -> bool:
        if scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            return True
        headers = Headers(scope=scope)
        site = headers.get("sec-fetch-site")
        if site is not None:
            return site.strip().lower() in ("same-origin", "none")
        origin = headers.get("origin")
        if origin is None:
            return True
        origin = origin.strip().lower()
        return origin == _own_origin(scope, headers) or origin in self._trusted(scope)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self.allowed(scope):
            response = PlainTextResponse(
                "Geweigerd: dit verzoek kwam van een andere website.", status_code=403
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def body_limit(path: str) -> int:
    return PUBLIC_MAX_BODY_BYTES if path in PUBLIC_BODY_PATHS else MAX_BODY_BYTES


class BodySizeLimit:
    """Refuse request bodies over the limit before anything parses them.

    A too large ``Content-Length`` is answered with 413 straight away; a streamed (chunked)
    body is counted while it is read and cut off at the limit.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            declared = declared.strip()
            if not declared.isascii() or not declared.isdigit():
                await PlainTextResponse("Invalid Content-Length", 400)(scope, receive, send)
                return
            if int(declared) > limit:
                await self._too_large(scope, receive, send)
                return

        received = 0
        too_large = False  # the streamed body went over the limit
        started = False  # a response start went out to the client
        replaced = False  # we answered 413 instead of the app

        async def counting_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Stop reading: to the app it looks like the client went away.
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started, replaced
            if replaced:
                return
            if message["type"] == "http.response.start" and too_large and not started:
                replaced = True
                started = True
                await self._too_large(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not too_large or started:
                raise
        if too_large and not started:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse("Request body too large", status_code=413)
        response.headers["Connection"] = "close"
        await response(scope, receive, send)


class SecurityHeaders:
    """Anti-framing, CSP, nosniff, referrer policy, HSTS over https, no-store on login pages."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.untrusted_proxy_warned = False

    def _check_proxy(self, scope: Scope) -> None:
        """Warn once when a proxy says https but uvicorn did not trust it (FORWARDED_ALLOW_IPS):
        cookies then go out without Secure and every visitor shares the proxy's throttle."""
        if self.untrusted_proxy_warned or scope.get("scheme") != "http":
            return
        proto = Headers(scope=scope).get("x-forwarded-proto", "")
        if proto.split(",")[-1].strip().lower() != "https":
            return
        self.untrusted_proxy_warned = True
        peer = (scope.get("client") or ("unknown",))[0]
        log.warning(
            "a proxy at %s forwards https requests but is not in FORWARDED_ALLOW_IPS: the app "
            "sees plain http, so session cookies go out without Secure, there is no HSTS and "
            "every visitor shares one login throttle; add its address to FORWARDED_ALLOW_IPS",
            peer,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self._check_proxy(scope)
        path = scope.get("path", "")
        https = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                csp = (
                    DOCS_CONTENT_SECURITY_POLICY
                    if path.startswith(DOCS_PATHS)
                    else CONTENT_SECURITY_POLICY
                )
                headers.setdefault("Content-Security-Policy", csp)
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "same-origin")
                if https:
                    headers.setdefault("Strict-Transport-Security", HSTS)
                if path.startswith(NO_STORE_PREFIXES):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)
