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
        return_value=Response(200, json=[{"market": "BTC-EUR", "price": "40000"},
                                         {"market": "ETH-EUR", "price": "2000"},
                                         {"market": "XRP-EUR", "price": "0.5"}])
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
