"""Thin async client for the Bitvavo REST API (v2).

Only the endpoints the app needs are wrapped. Authentication follows the Bitvavo
spec: HMAC-SHA256 over ``timestamp + method + path + body`` with the API secret.
See https://docs.bitvavo.com/ for the full reference.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx


class BitvavoError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        code = payload.get("errorCode") if isinstance(payload, dict) else None
        msg = payload.get("error") if isinstance(payload, dict) else str(payload)
        super().__init__(f"Bitvavo {status} (code {code}): {msg}")


def sign(secret: str, timestamp: int, method: str, path: str, body: str = "") -> str:
    """Return the hex HMAC-SHA256 signature Bitvavo expects in Bitvavo-Access-Signature.

    ``path`` is the part after the host, including ``/v2`` and any query string.
    ``body`` must be the exact JSON string that is sent (empty for GET/DELETE).
    """
    message = f"{timestamp}{method.upper()}{path}{body}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class BitvavoClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.bitvavo.com/v2",
        access_window_ms: int = 10_000,
        timeout: float = 10.0,
    ):
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self._window = access_window_ms
        self._http = httpx.AsyncClient(timeout=timeout)
        # Bitvavo reports remaining weight per minute in this header; keep the last value.
        self.rate_limit_remaining: int | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def authenticated(self) -> bool:
        return bool(self._key and self._secret)

    # -- plumbing -------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
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
        resp = await self._http.request(
            method, url, params=params, content=body_str or None, headers=headers
        )
        remaining = resp.headers.get("bitvavo-ratelimit-remaining")
        if remaining is not None and remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text
        if resp.status_code >= 400 or (isinstance(payload, dict) and "errorCode" in payload):
            raise BitvavoError(resp.status_code, payload)
        return payload

    # -- public endpoints -------------------------------------------------------------

    async def time(self) -> int:
        return int((await self._request("GET", "/time"))["time"])

    async def markets(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/markets")

    async def ticker_price(self, market: str) -> float:
        data = await self._request("GET", "/ticker/price", params={"market": market})
        return float(data["price"])

    async def ticker_prices(self, markets: list[str]) -> dict[str, float]:
        data = await self._request("GET", "/ticker/price")
        wanted = set(markets)
        return {d["market"]: float(d["price"]) for d in data if d["market"] in wanted}

    async def candles(
        self, market: str, interval: str = "1h", limit: int = 200
    ) -> list[list[float]]:
        """Return candles as [timestamp_ms, open, high, low, close, volume], oldest first."""
        raw = await self._request(
            "GET", f"/{market}/candles", params={"interval": interval, "limit": limit}
        )
        rows = [[int(c[0]), *map(float, c[1:6])] for c in raw]
        rows.sort(key=lambda r: r[0])
        return rows

    # -- authenticated endpoints -------------------------------------------------------

    async def balance(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/balance", auth=True)

    async def place_order(
        self,
        market: str,
        side: str,
        order_type: str = "market",
        amount: float | None = None,
        amount_quote: float | None = None,
        price: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"market": market, "side": side, "orderType": order_type}
        if amount is not None:
            body["amount"] = f"{amount:.8f}".rstrip("0").rstrip(".")
        if amount_quote is not None:
            body["amountQuote"] = f"{amount_quote:.2f}"
        if price is not None:
            body["price"] = f"{price:.8f}".rstrip("0").rstrip(".")
        return await self._request("POST", "/order", body=body, auth=True)

    async def open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        params = {"market": market} if market else None
        return await self._request("GET", "/ordersOpen", params=params, auth=True)

    async def cancel_order(self, market: str, order_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", "/order", params={"market": market, "orderId": order_id}, auth=True
        )
