import json

import pytest
import respx
from httpx import Response

from app.bitvavo.client import BitvavoClient, BitvavoError

BASE = "https://api.bitvavo.com/v2"


@pytest.fixture
async def client():
    c = BitvavoClient("key", "secret", BASE)
    yield c
    await c.aclose()


@respx.mock
async def test_ticker_price(client):
    respx.get(f"{BASE}/ticker/price", params={"market": "BTC-EUR"}).mock(
        return_value=Response(200, json={"market": "BTC-EUR", "price": "61234.5"})
    )
    assert await client.ticker_price("BTC-EUR") == 61234.5


@respx.mock
async def test_candles_sorted_oldest_first(client):
    respx.get(f"{BASE}/BTC-EUR/candles").mock(
        return_value=Response(
            200,
            json=[
                [2000, "2", "3", "1", "2.5", "10"],
                [1000, "1", "2", "0.5", "1.5", "5"],
            ],
        )
    )
    rows = await client.candles("BTC-EUR", "1h", 2)
    assert [r[0] for r in rows] == [1000, 2000]
    assert rows[0] == [1000, 1.0, 2.0, 0.5, 1.5, 5.0]


@respx.mock
async def test_authenticated_request_sets_headers_and_body(client):
    route = respx.post(f"{BASE}/order").mock(
        return_value=Response(200, json={"orderId": "abc", "status": "filled"})
    )
    await client.place_order("BTC-EUR", "buy", amount_quote=25)
    req = route.calls.last.request
    assert req.headers["Bitvavo-Access-Key"] == "key"
    assert len(req.headers["Bitvavo-Access-Signature"]) == 64
    assert req.headers["Bitvavo-Access-Window"] == "10000"
    assert json.loads(req.content) == {
        "market": "BTC-EUR",
        "side": "buy",
        "orderType": "market",
        "amountQuote": "25.00",
    }


@respx.mock
async def test_error_payload_raises(client):
    respx.get(f"{BASE}/balance").mock(
        return_value=Response(403, json={"errorCode": 305, "error": "No active API key"})
    )
    with pytest.raises(BitvavoError) as exc:
        await client.balance()
    assert exc.value.status == 403
    assert "305" in str(exc.value)


async def test_auth_without_credentials_fails_fast():
    c = BitvavoClient()
    with pytest.raises(BitvavoError):
        await c.balance()
    await c.aclose()


@respx.mock
async def test_rate_limit_header_tracked(client):
    respx.get(f"{BASE}/time").mock(
        return_value=Response(200, json={"time": 1}, headers={"bitvavo-ratelimit-remaining": "987"})
    )
    await client.time()
    assert client.rate_limit_remaining == 987
