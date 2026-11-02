"""Thin async client for the Bitvavo REST API (v2).

Only the endpoints the app needs are wrapped. Authentication follows the Bitvavo
spec: HMAC-SHA256 over ``timestamp + method + path + body`` with the API secret.
See https://docs.bitvavo.com/ for the full reference.

The client also protects the account against Bitvavo's rate limits: it follows the
``bitvavo-ratelimit-*`` response headers, backs off after a 429 or a ban, and caches
candles for a few seconds so several loops asking for the same data cost one request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Bitvavo market notation: BASE-QUOTE, e.g. BTC-EUR. Checked before a market goes into a
# path or query, so a crafted value can never point a request at another endpoint.
MARKET_RE = re.compile(r"^[A-Z0-9]{1,15}-[A-Z]{2,5}$")

# Bitvavo error codes that mean "too many requests": 103 rate limited, 104 too many new
# orders, 105 banned (the message holds the expiry in ms).
RATE_LIMIT_CODES = {103, 104, 105}
# Below this remaining weight, non-critical calls (candles) wait for the window to reset.
RATE_LIMIT_FLOOR = 25
MAX_BACKOFF_S = 300.0
# "No order found" (also returned briefly while an order is still being processed).
ORDER_NOT_FOUND_CODE = 240


class BitvavoError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        code = payload.get("errorCode") if isinstance(payload, dict) else None
        self.code = code
        msg = payload.get("error") if isinstance(payload, dict) else str(payload)
        super().__init__(f"Bitvavo {status} (code {code}): {msg}")

    @property
    def ambiguous(self) -> bool:
        """True when Bitvavo may have executed the request anyway (no response, or a 5xx)."""
        return self.status == 0 or self.status >= 500

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or self.code in RATE_LIMIT_CODES


class BitvavoNetworkError(BitvavoError):
    """The request got no response: timeout, connection reset, DNS failure."""

    def __init__(self, exc: Exception):
        super().__init__(0, {"error": f"{type(exc).__name__}: {exc}"})


class InvalidMarketError(BitvavoError, ValueError):
    """A market name that is not in Bitvavo notation; nothing was sent."""

    def __init__(self, market: object):
        super().__init__(400, {"error": f"invalid market {str(market)[:40]!r}"})


def check_market(market: str) -> str:
    """Return ``market`` when it is in Bitvavo notation (BTC-EUR); raise otherwise."""
    if not isinstance(market, str) or not MARKET_RE.match(market):
        raise InvalidMarketError(market)
    return market


def sign(secret: str, timestamp: int, method: str, path: str, body: str = "") -> str:
    """Return the hex HMAC-SHA256 signature Bitvavo expects in Bitvavo-Access-Signature.

    ``path`` is the part after the host, including ``/v2`` and any query string.
    ``body`` must be the exact JSON string that is sent (empty for GET/DELETE).
    """
    message = f"{timestamp}{method.upper()}{path}{body}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _format_amount(amount: float | str) -> str:
    if isinstance(amount, str):
        return amount
    return f"{amount:.8f}".rstrip("0").rstrip(".")


def _epoch_header(headers: httpx.Headers, *names: str) -> float | None:
    for name in names:
        value = headers.get(name)
        if value is not None and value.strip().isdigit():
            return int(value) / 1000
    return None


class BitvavoClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.bitvavo.com/v2",
        access_window_ms: int = 10_000,
        timeout: float = 10.0,
        candle_ttl_s: float = 20.0,
        markets_ttl_s: float = 3600.0,
    ):
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self._window = access_window_ms
        self._http = httpx.AsyncClient(timeout=timeout)
        # Bitvavo reports remaining weight per minute in this header; keep the last value.
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: float | None = None  # epoch seconds
        # Client-side backoff after a 429 or a ban: no request is sent before this moment.
        self.blocked_until: float = 0.0
        self._backoff_s = 0.0
        self._candle_ttl = candle_ttl_s
        self._candle_cache: dict[tuple[str, str, int], tuple[float, list[list[float]]]] = {}
        self._markets_ttl = markets_ttl_s
        self._markets_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def authenticated(self) -> bool:
        return bool(self._key and self._secret)

    # -- rate limiting ----------------------------------------------------------------

    def _note_headers(self, headers: httpx.Headers) -> None:
        remaining = headers.get("bitvavo-ratelimit-remaining")
        if remaining is not None and remaining.strip().isdigit():
            self.rate_limit_remaining = int(remaining)
        reset = _epoch_header(headers, "bitvavo-ratelimit-resetat")
        if reset is not None:
            self.rate_limit_reset_at = reset

    def _back_off(self, exc: BitvavoError, headers: httpx.Headers) -> None:
        now = time.time()
        self._backoff_s = min(max(self._backoff_s * 2, 5.0), MAX_BACKOFF_S)
        until = now + self._backoff_s
        allowed = _epoch_header(headers, "bitvavo-ratelimit-allowat", "bitvavo-ratelimit-resetat")
        if allowed is not None:
            until = max(until, allowed)
        if exc.code == 105 and isinstance(exc.payload, dict):
            # "... banned ... The ban expires at 1700000000000" (milliseconds)
            found = re.findall(r"\d{12,}", str(exc.payload.get("error", "")))
            if found:
                until = max(until, int(found[-1]) / 1000)
        self.blocked_until = min(until, now + 3600)
        log.warning(
            "Bitvavo rate limit (%s); no requests until %s UTC",
            exc,
            time.strftime("%H:%M:%S", time.gmtime(self.blocked_until)),
        )

    async def _respect_limits(self, critical: bool) -> None:
        now = time.time()
        if now < self.blocked_until:
            raise BitvavoError(
                429,
                {
                    "errorCode": 103,
                    "error": "waiting after a Bitvavo rate limit until "
                    + time.strftime("%H:%M:%S UTC", time.gmtime(self.blocked_until)),
                },
            )
        if (
            not critical
            and self.rate_limit_remaining is not None
            and self.rate_limit_remaining < RATE_LIMIT_FLOOR
            and self.rate_limit_reset_at is not None
            and self.rate_limit_reset_at > now
        ):
            # Keep the last of the budget for prices, orders and exits: wait for the reset
            # when it is close, otherwise refuse without sending anything.
            wait = self.rate_limit_reset_at - now
            if wait > 5:
                raise BitvavoError(
                    429, {"errorCode": 103, "error": "rate-limit budget nearly used up"}
                )
            await asyncio.sleep(wait)

    # -- plumbing -------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        critical: bool = False,
    ) -> Any:
        await self._respect_limits(critical or auth)
        url = f"{self._base}{endpoint}"
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {"Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.authenticated:
                raise BitvavoError(401, {"error": "No Bitvavo API key/secret configured"})
            # The signed path must match the request exactly, including the query string.
            query = str(httpx.QueryParams(params)) if params else ""
            path = f"/v2{endpoint}" + (f"?{query}" if query else "")
            ts = int(time.time() * 1000)
            headers.update(
                {
                    "Bitvavo-Access-Key": self._key,
                    "Bitvavo-Access-Timestamp": str(ts),
                    "Bitvavo-Access-Signature": sign(self._secret, ts, method, path, body_str),
                    "Bitvavo-Access-Window": str(self._window),
                }
            )
        try:
            resp = await self._http.request(
                method, url, params=params, content=body_str or None, headers=headers
            )
        except httpx.HTTPError as exc:
            raise BitvavoNetworkError(exc) from exc
        self._note_headers(resp.headers)
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        if resp.status_code >= 400 or (isinstance(payload, dict) and "errorCode" in payload):
            err = BitvavoError(resp.status_code, payload)
            if err.rate_limited:
                self._back_off(err, resp.headers)
            raise err
        self._backoff_s = 0.0
        return payload

    # -- public endpoints -------------------------------------------------------------

    async def time(self) -> int:
        return int((await self._request("GET", "/time"))["time"])

    async def markets(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/markets")

    async def market_info(self, market: str) -> dict[str, Any] | None:
        """One market's trading rules (precision, minimum order size), cached for an hour."""
        check_market(market)
        now = time.monotonic()
        if self._markets_cache is None or self._markets_cache[0] <= now:
            rows = await self._request("GET", "/markets", critical=True)
            by_market = {r["market"]: r for r in rows if isinstance(r, dict) and "market" in r}
            self._markets_cache = (now + self._markets_ttl, by_market)
        return self._markets_cache[1].get(market)

    async def ticker_price(self, market: str) -> float:
        check_market(market)
        data = await self._request("GET", "/ticker/price", params={"market": market}, critical=True)
        return float(data["price"])

    async def ticker_prices(self, markets: list[str]) -> dict[str, float]:
        data = await self._request("GET", "/ticker/price", critical=True)
        wanted = set(markets)
        return {d["market"]: float(d["price"]) for d in data if d["market"] in wanted}

    async def candles(
        self, market: str, interval: str = "1h", limit: int = 200
    ) -> list[list[float]]:
        """Return candles as [timestamp_ms, open, high, low, close, volume], oldest first.

        Results are cached per (market, interval, limit) for ``candle_ttl_s`` seconds, so the
        cycle, the dip watch and the dashboard share one request.
        """
        check_market(market)
        key = (market, str(interval), int(limit))
        now = time.monotonic()
        hit = self._candle_cache.get(key)
        if hit is not None and hit[0] > now:
            return [list(r) for r in hit[1]]
        raw = await self._request(
            "GET", f"/{market}/candles", params={"interval": interval, "limit": limit}
        )
        rows = [[int(c[0]), *map(float, c[1:6])] for c in raw]
        rows.sort(key=lambda r: r[0])
        if self._candle_ttl > 0:
            if len(self._candle_cache) > 256:
                self._candle_cache = {k: v for k, v in self._candle_cache.items() if v[0] > now}
            self._candle_cache[key] = (now + self._candle_ttl, rows)
        return [list(r) for r in rows]

    # -- authenticated endpoints -------------------------------------------------------

    async def balance(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/balance", auth=True)

    async def account(self) -> dict[str, Any]:
        """Account info incl. fee tier: {"fees": {"taker": "0.0025", "maker": "0.0015"}}."""
        return await self._request("GET", "/account", auth=True)

    async def taker_fee_pct(self) -> float | None:
        """Current taker fee in percent, or None when it cannot be read (also offline)."""
        try:
            fees = (await self.account()).get("fees") or {}
            return float(fees["taker"]) * 100
        except (BitvavoError, KeyError, TypeError, ValueError, AttributeError):
            return None

    async def place_order(
        self,
        market: str,
        side: str,
        order_type: str = "market",
        amount: float | str | None = None,
        amount_quote: float | None = None,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        check_market(market)
        body: dict[str, Any] = {"market": market, "side": side, "orderType": order_type}
        if amount is not None:
            body["amount"] = _format_amount(amount)
        if amount_quote is not None:
            body["amountQuote"] = f"{amount_quote:.2f}"
        if price is not None:
            body["price"] = f"{price:.8f}".rstrip("0").rstrip(".")
        if client_order_id is not None:
            body["clientOrderId"] = client_order_id
        return await self._request("POST", "/order", body=body, auth=True)

    async def get_order(
        self, market: str, order_id: str | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Look an order up by Bitvavo's orderId or by our own clientOrderId."""
        check_market(market)
        params: dict[str, Any] = {"market": market}
        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["clientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return await self._request("GET", "/order", params=params, auth=True)

    async def open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        params = {"market": check_market(market)} if market else None
        return await self._request("GET", "/ordersOpen", params=params, auth=True)

    async def cancel_order(self, market: str, order_id: str) -> dict[str, Any]:
        check_market(market)
        return await self._request(
            "DELETE", "/order", params={"market": market, "orderId": order_id}, auth=True
        )
