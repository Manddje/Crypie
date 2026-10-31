import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.main import app

BASE = "https://api.bitvavo.com/v2"


@pytest.fixture
async def api():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_health(api):
    r = await api.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "mode": "paper"}


@respx.mock
async def test_paper_order_flow(api):
    # Specific route first: respx matches in registration order.
    respx.get(f"{BASE}/ticker/price", params={"market": "ETH-EUR"}).mock(
        return_value=Response(200, json={"market": "ETH-EUR", "price": "2000"})
    )
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(
            200,
            json=[
                {"market": "BTC-EUR", "price": "40000"},
                {"market": "ETH-EUR", "price": "2000"},
                {"market": "XRP-EUR", "price": "0.5"},
            ],
        )
    )

    r = await api.get("/api/prices")
    assert r.json() == {"BTC-EUR": 40000.0, "ETH-EUR": 2000.0}

    body = {"market": "eth-eur", "side": "buy", "amount_quote": 100}
    r = await api.post("/api/orders", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "paper" and r.json()["amount"] == pytest.approx(0.05)

    r = await api.get("/api/balance")
    assert r.json()["balances"] == {"EUR": 900.0, "ETH": 0.05}

    r = await api.post("/api/orders", json={"market": "XRP-EUR", "side": "buy", "amount_quote": 10})
    assert r.status_code == 400

    r = await api.get("/")
    assert r.status_code == 200 and "ETH-EUR" in r.text


def _candles(n=120, start=30000.0):
    out = []
    price = start
    for i in range(n):
        price *= 1.003 if (i // 10) % 2 == 0 else 0.997
        out.append(
            [
                1_700_000_000_000 + i * 3_600_000,
                str(price),
                str(price * 1.01),
                str(price * 0.99),
                str(price),
                "5",
            ]
        )
    return out


@respx.mock
async def test_strategy_roundtrip_and_backtest(api):
    r = await api.get("/api/strategy")
    assert r.status_code == 200 and r.json()["name"] == "RSI mean reversion"

    new = r.json() | {"name": "Mijn test", "market": "ETH-EUR"}
    r = await api.put("/api/strategy", json=new)
    assert r.status_code == 200, r.text
    assert (await api.get("/api/strategy")).json()["name"] == "Mijn test"

    bad = new | {"buy": [{"left": "bestaat_niet", "op": ">", "right": 1}]}
    assert (await api.put("/api/strategy", json=bad)).status_code == 422

    r = await api.put("/api/strategy", json=new | {"market": "XRP-EUR"})
    assert r.status_code == 400

    respx.get(f"{BASE}/ETH-EUR/candles").mock(return_value=Response(200, json=_candles()))
    r = await api.post("/api/backtest", json={"limit": 120})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["market"] == "ETH-EUR" and body["candles"] == 120
    assert {"return_pct", "buy_hold_return_pct", "latest", "trade_log"} <= set(body)

    r = await api.get("/strategy")
    assert r.status_code == 200 and "Mijn test" in r.text

    r = await api.post("/api/strategy/propose", json={"description": "koop laag verkoop hoog"})
    assert r.status_code == 503  # no ANTHROPIC_API_KEY in tests

    r = await api.get("/api/claude/usage")
    assert r.json()["limit_usd"] == 100.0 and r.json()["spent_usd"] == 0
