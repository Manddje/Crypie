from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.db import Decision, Order, SessionLocal, init_db
from app.main import app
from app.trading.history import (
    bar_time,
    candle_dicts,
    market_actions,
    period_spec,
    portfolio_history,
)

BASE = "https://api.bitvavo.com/v2"
T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)


def _order(session, market, side, amount, quote, price, at, reason=None, mode="paper"):
    o = Order(
        created_at=at,
        mode=mode,
        market=market,
        side=side,
        order_type="market",
        amount=amount,
        amount_quote=quote,
        price=price,
        status="filled",
        reason=reason,
    )
    session.add(o)
    session.commit()
    return o


def _candles(closes):
    return [
        {
            "time": int((T0 + timedelta(hours=i)).timestamp()),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
        }
        for i, close in enumerate(closes)
    ]


def test_period_and_bar_time():
    assert period_spec("30d") == ("30d", "4h", 180)
    assert period_spec("nonsense") == ("7d", "1h", 168)
    moment = datetime(2026, 11, 1, 13, 47, tzinfo=UTC)
    assert bar_time(moment, "1h") == int(datetime(2026, 11, 1, 13, 0, tzinfo=UTC).timestamp())
    assert bar_time(moment, "1d") == int(T0.timestamp())
    assert candle_dicts([[1_700_000_000_000, 1, 2, 0.5, 1.5, 9]])[0] == {
        "time": 1_700_000_000,
        "open": 1,
        "high": 2,
        "low": 0.5,
        "close": 1.5,
        "volume": 9,
    }


def test_portfolio_history_rewinds_orders_from_the_current_balance():
    init_db()
    with SessionLocal() as s:
        s.query(Order).delete()
        s.commit()
        # Bought 0.5 BTC for 100 EUR at hour 2, sold half of it for 60 EUR at hour 4.
        _order(s, "BTC-EUR", "buy", 0.5, 100.0, 200.0, T0 + timedelta(hours=2, minutes=10))
        _order(s, "BTC-EUR", "sell", 0.25, 60.0, 240.0, T0 + timedelta(hours=4, minutes=5))
        balances = {"EUR": 960.0, "BTC": 0.25}
        candles = {"BTC-EUR": _candles([200.0, 200.0, 200.0, 220.0, 240.0, 240.0])}
        points = portfolio_history(s, "paper", balances, candles, T0)
        s.query(Order).delete()
        s.commit()
    values = [p["value"] for p in points]
    # Hours 0-2: 1000 EUR cash. Hour 3: 900 EUR + 0.5 BTC at 220. Hour 4: the sell at 4:05 is
    # still ahead, so 900 + 0.5 * 240. Hour 5: 960 + 0.25 * 240.
    assert values == [1000.0, 1000.0, 1000.0, 1010.0, 1020.0, 1020.0]
    assert points[0]["time"] == int(T0.timestamp())


def test_market_actions_keeps_orders_claude_and_blocked_only():
    init_db()
    with SessionLocal() as s:
        s.query(Order).delete()
        s.query(Decision).delete()
        s.commit()
        _order(s, "BTC-EUR", "buy", 0.001, 50.0, 50000.0, T0, reason="claude: dip met goed nieuws")
        later = T0 + timedelta(hours=3)
        _order(s, "BTC-EUR", "sell", 0.001, 48.0, 48000.0, later, reason="stop_loss")
        _order(s, "ETH-EUR", "buy", 0.01, 20.0, 2000.0, T0, reason="rules: signaal")
        common = dict(mode="paper", market="BTC-EUR", price=50000.0, signal="hold")
        s.add_all(
            [
                Decision(
                    created_at=T0 + timedelta(hours=1), source="rules", action="hold", **common
                ),
                Decision(
                    created_at=T0 + timedelta(hours=2),
                    source="claude",
                    action="hold",
                    reasoning="Nieuws is gemengd.",
                    **common,
                ),
                Decision(
                    created_at=T0 + timedelta(hours=2, minutes=30),
                    source="rules",
                    action="buy",
                    blocked_by="daglimiet bereikt",
                    **common,
                ),
            ]
        )
        s.commit()
        actions = market_actions(s, "paper", "BTC-EUR", T0 - timedelta(hours=1), "1h")
        s.query(Order).delete()
        s.query(Decision).delete()
        s.commit()
    assert [a["kind"] for a in actions] == ["buy", "advice", "blocked", "sell"]
    assert actions[0]["label"] == "Gekocht op advies van Claude"
    assert actions[0]["detail"] == "dip met goed nieuws" and actions[0]["short"] == "Koop"
    assert actions[1]["label"] == "Claude adviseert afwachten" and actions[1]["executed"] is False
    assert actions[2]["label"] == "Kopen geblokkeerd"
    assert actions[2]["detail"] == "daglimiet bereikt"
    assert actions[3]["label"] == "Verkocht: stop-loss geraakt"
    assert actions[3]["short"] == "Stop-loss"
    assert actions[3]["bar_time"] == int((T0 + timedelta(hours=3)).timestamp())


@pytest.fixture
async def api():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    # The in-memory database outlives the test; leave it as the other API tests expect it.
    with SessionLocal() as s:
        s.query(Order).delete()
        s.query(Decision).delete()
        s.commit()


@respx.mock
async def test_chart_endpoints_and_dashboard(api):
    now_ms = int(datetime.now(UTC).timestamp()) // 3600 * 3600 * 1000
    rows = [[now_ms - (167 - i) * 3_600_000, "100", "101", "99", "100", "1"] for i in range(168)]
    respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=rows))
    respx.get(f"{BASE}/ETH-EUR/candles").mock(return_value=Response(200, json=rows))
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(
            200, json=[{"market": "BTC-EUR", "price": "100"}, {"market": "ETH-EUR", "price": "100"}]
        )
    )
    with SessionLocal() as s:
        s.query(Order).delete()
        s.commit()
        _order(
            s,
            "BTC-EUR",
            "buy",
            0.5,
            50.0,
            100.0,
            datetime.now(UTC) - timedelta(hours=5),
            reason="rules: koopsignaal",
        )

    r = await api.get("/api/chart/btc-eur?period=7d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["market"] == "BTC-EUR" and body["interval"] == "1h"
    assert len(body["candles"]) == 168 and body["candles"][0]["time"] == rows[0][0] // 1000
    assert [a["kind"] for a in body["actions"]] == ["buy"]
    assert body["actions"][0]["label"] == "Gekocht op strategiesignaal"
    assert (await api.get("/api/chart/DOGE-EUR")).status_code == 404

    r = await api.get("/api/portfolio/history?period=7d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start_eur"] == 1000.0 and len(body["points"]) == 168
    assert len(body["actions"]) == 1
    # The paper wallet is untouched (1000 EUR, no coins): rewinding the inserted buy adds the
    # 50 EUR back and takes the 0.5 BTC away for the hours before it.
    assert body["points"][-1]["value"] == 1000.0
    assert body["points"][0]["value"] == 1000.0  # 1050 EUR minus 0.5 BTC at 100 EUR
    assert body["points"][0]["value"] == 1050.0 - 0.5 * 100.0

    r = await api.get("/")
    assert r.status_code == 200
    assert "Koers en acties" in r.text and "Portefeuillewaarde" in r.text
    assert "lightweight-charts.standalone.production.js" in r.text
    assert 'data-period="90d"' in r.text
