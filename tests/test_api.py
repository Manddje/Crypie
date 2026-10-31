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
    # 100 EUR minus the 0.25% taker fee buys 99.75 / 2000 ETH.
    assert r.json()["mode"] == "paper" and r.json()["amount"] == pytest.approx(0.049875)
    assert r.json()["fee_quote"] == pytest.approx(0.25)

    r = await api.get("/api/balance")
    balances = r.json()["balances"]
    assert balances["EUR"] == pytest.approx(900.0) and balances["ETH"] == pytest.approx(0.049875)

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


@respx.mock
async def test_news_endpoints(api):
    from pathlib import Path

    from app.news.sources import NewsFetcher

    fixture = Path(__file__).parent / "fixtures" / "feed.xml"
    # Test settings use the default feeds; stub them all with one fixture.
    for url in app.state.settings.news_rss_feeds:
        respx.get(url).mock(return_value=Response(200, content=fixture.read_bytes()))
    respx.get(NewsFetcher.GDELT_URL).mock(return_value=Response(429))

    r = await api.post("/api/news/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] >= 1 and body["assessed"] == 0  # no API key in tests
    assert "ANTHROPIC_API_KEY" in body["error"]

    r = await api.get("/api/news?hours=24")
    titles = [i["title"] for i in r.json()]
    assert "Binance & $6.3B hack sparks debate" in titles
    assert "Old item" not in titles

    r = await api.get("/api/news/sentiment")
    assert set(r.json()) == {"BTC", "ETH"}
    assert r.json()["BTC"]["label"] == "geen nieuws"

    r = await api.get("/news")
    assert r.status_code == 200 and "hack sparks debate" in r.text


@respx.mock
async def test_trading_endpoints(api):
    r = await api.get("/api/trading/status")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper" and body["paused"] is False
    assert body["limits"]["max_trade_eur"] == 100.0 and body["claude_available"] is False

    r = await api.post("/api/trading/pause", json={"paused": True})
    assert r.json() == {"paused": True}
    assert (await api.get("/api/trading/status")).json()["paused"] is True
    await api.post("/api/trading/pause", json={"paused": False})

    # A full cycle against mocked Bitvavo: flat candles -> hold, nothing executed.
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(
            200, json=[{"market": "BTC-EUR", "price": "100"}, {"market": "ETH-EUR", "price": "10"}]
        )
    )
    flat = [[1_700_000_000_000 + i * 3_600_000, "100", "101", "99", "100", "1"] for i in range(60)]
    respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=flat))
    respx.get(f"{BASE}/ETH-EUR/candles").mock(return_value=Response(200, json=flat))
    r = await api.post("/api/trading/cycle")
    assert r.status_code == 200, r.text
    assert [c["action"] for c in r.json()] == ["hold", "hold"]

    r = await api.get("/api/decisions")
    assert len(r.json()) == 2 and r.json()[0]["source"] == "rules"
    r = await api.get("/api/positions")
    assert r.json() == {"open": [], "closed": []}
    assert (await api.get("/api/evaluations")).json() == []
    assert (await api.post("/api/evaluations")).status_code == 503  # no key
    assert (await api.post("/api/evaluations/999/apply")).status_code == 404

    r = await api.get("/trading")
    assert r.status_code == 200 and "Harde limieten" in r.text
