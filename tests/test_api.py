import json
import re

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.db import Order, SessionLocal
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
    assert r.json() == {"status": "ok"}  # nothing about the instance without a login


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
    assert r.status_code == 422 and r.json()["code"] == "order_refused"

    r = await api.get("/")
    assert r.status_code == 200 and "ETH-EUR" in r.text
    # Portfolio value = cash + coins at the current price, shown in Dutch notation.
    # The 0.25 EUR fee of the order above is gone for good, so 999,75 instead of 1.000,00.
    assert "€\u00a0999,75" in r.text and "Oefenmodus" in r.text

    # The manual buy opened a position (protected by the strategy's stops); selling closes it.
    (pos,) = (await api.get("/api/positions")).json()["open"]
    assert pos["market"] == "ETH-EUR" and pos["amount"] == pytest.approx(0.049875)
    r = await api.post(
        "/api/orders", json={"market": "ETH-EUR", "side": "sell", "amount_quote": 99}
    )
    assert r.status_code == 201, r.text
    assert r.json()["amount"] == pytest.approx(0.049875)  # the rest would be below € 5: all
    assert (await api.get("/api/positions")).json()["open"] == []

    r = await api.get("/static/app.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]


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
    assert r.status_code == 200 and r.json()["name"] == "RSI dip in opwaartse trend"

    new = r.json() | {"name": "Mijn test", "market": "ETH-EUR"}
    r = await api.put("/api/strategy", json=new)
    assert r.status_code == 200, r.text
    assert (await api.get("/api/strategy")).json()["name"] == "Mijn test"

    bad = new | {"buy": [{"left": "bestaat_niet", "op": ">", "right": 1}]}
    assert (await api.put("/api/strategy", json=bad)).status_code == 422

    r = await api.put("/api/strategy", json=new | {"market": "XRP-EUR"})
    assert r.status_code == 422 and r.json()["code"] == "market_not_tracked"

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

    r = await api.post(
        "/strategy", data={"action": "reset", "strategy_json": ""}, follow_redirects=True
    )
    assert r.status_code == 200 and "hersteld en actief" in r.text
    assert "trailing stop" in r.text.lower()
    assert (await api.get("/api/strategy")).json()["name"] == "RSI dip in opwaartse trend"


@respx.mock
async def test_news_endpoints(api):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime
    from pathlib import Path

    from app.news.sources import NewsFetcher

    # The fixture carries a fixed publication date; the API filters on "last 24 hours" and the
    # store drops anything older than NEWS_MAX_AGE_HOURS, so stamp the hack item with a fresh
    # date on every run instead of letting the test expire with the calendar.
    fresh = format_datetime(datetime.now(UTC) - timedelta(hours=1))
    feed = (
        (Path(__file__).parent / "fixtures" / "feed.xml")
        .read_text()
        .replace("Sat, 31 Oct 2026 09:58:50 +0000", fresh)
        .encode()
    )
    # Test settings use the default feeds; stub them all with one fixture.
    for url in app.state.settings.news_rss_feeds:
        respx.get(url).mock(return_value=Response(200, content=feed))
    respx.get(NewsFetcher.GDELT_URL).mock(return_value=Response(429))
    respx.get(url__startswith="https://cryptocurrency.cv/").mock(return_value=Response(503))
    respx.get(url__startswith="https://news.google.com/").mock(
        return_value=Response(200, content=feed)
    )
    respx.get(NewsFetcher.FEAR_GREED_URL).mock(
        return_value=Response(200, json={"data": [{"value": "30", "value_classification": "Fear"}]})
    )

    r = await api.post("/api/news/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] >= 1 and body["assessed"] == 0  # no API key in tests
    # A missing key is a choice, not a failed run (UX-41): said in 'assessing', not 'error'.
    assert body["assessing"] is False and body["error"] is None

    r = await api.get("/api/news?hours=24")
    titles = [i["title"] for i in r.json()]
    assert "Binance & $6.3B hack sparks debate" in titles
    assert "Old item" not in titles
    first = r.json()[0]
    assert first["counts"] is False and first["counts_reason"] == "nog niet beoordeeld door Claude"
    r = await api.get("/api/news?hours=24&counts=true")
    assert r.json() == []  # nothing assessed without an API key, so nothing counts yet

    r = await api.get("/api/news/sentiment")
    assert set(r.json()) == {"BTC", "ETH"}
    assert r.json()["BTC"]["label"] == "geen nieuws"

    r = await api.get("/news")
    assert r.status_code == 200 and "hack sparks debate" in r.text
    assert "Fear" in r.text and "30" in r.text  # Fear & Greed index shown
    # Without a key nothing waits for Claude, so the page does not say 'nog niet' (UX-45).
    assert "telt niet mee" in r.text and "niet beoordeeld (geen Claude-sleutel)" in r.text
    r = await api.get("/news?hours=24&telt=ja")
    assert r.status_code == 200 and "hack sparks debate" not in r.text
    assert "Geen nieuwsfeiten die meetellen" in r.text
    r = await api.get("/news?hours=24&telt=nee")
    assert "hack sparks debate" in r.text


@respx.mock
async def test_trading_endpoints(api):
    r = await api.get("/api/trading/status")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper" and body["paused"] is False
    assert body["limits"]["max_trade_eur"] == 100.0 and body["claude_available"] is False
    assert body["watch"] == [] and body["watch_interval_min"] == 5

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
    assert r.json()["open"] == []
    assert (await api.get("/api/evaluations")).json() == []
    assert (await api.post("/api/evaluations")).status_code == 503  # no key
    assert (await api.post("/api/evaluations/999/apply")).status_code == 404

    r = await api.get("/trading")
    assert r.status_code == 200 and "Harde limieten" in r.text
    assert 'id="trend-title"' not in r.text  # nothing computed yet
    assert "Agenda en risk-off" in r.text and "Geen agendamoment actief" in r.text

    from app.db import SessionLocal
    from app.trading.trend import save_trend

    with SessionLocal() as s:
        save_trend(
            s,
            {
                "market": "BTC-EUR",
                "wma200": 30000.0,
                "weeks": 210,
                "above_wma200": True,
                "vs_wma200_pct": 33.3,
                "monthly_rsi": 56.0,
                "months": 40,
                "monthly_rsi_above_50": True,
                "regime": "up",
                "regime_nl": "opwaarts",
            },
        )
    r = await api.get("/trading")
    assert "Lange-termijntrend" in r.text and "opwaarts" in r.text and "+33,3%" in r.text
    assert "Laatste cyclus per markt" in r.text and "Alle cycli" in r.text
    # Flat candles keep the RSI at a neutral 50: no buy and no sell signal, so hold, summed up in
    # one line and explained per condition (with readable indicator names) in the checklist.
    assert "0 van 2 koopvoorwaarden gehaald" in r.text
    assert "niet gehaald: </span>RSI 14 lager dan 35" in r.text
    assert "laatste 7 dagen" in r.text


@respx.mock
async def test_settings_page_and_api_change_the_running_app(api):
    r = await api.get("/settings")
    assert r.status_code == 200 and "Instellingen" in r.text and "Harde limieten" in r.text

    r = await api.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper" and body["values"]["max_trade_eur"] == 100.0
    assert "ANTHROPIC_API_KEY" not in json.dumps(body)  # secrets are never part of it
    assert body["live_blocker"]  # no Bitvavo key and no ack in tests

    # Save via the form: limits are applied to the engine at once, without a restart.
    form = {
        "group": "limits",
        "max_trade_eur": "40",
        "min_trade_eur": "10",
        "max_exposure_pct": "30",
        "daily_loss_limit_pct": "3",
        "max_trades_per_day": "2",
        "cooldown_min_after_stop": "60",
    }
    # The cooldown goes from 240 to 60 minutes: more than twice as loose, so it asks first.
    r = await api.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 409 and "Afkoelperiode na stop-loss" in r.text
    form["confirm_loosen"] = "1"
    r = await api.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?saved=limits")
    status = (await api.get("/api/trading/status")).json()
    assert status["limits"]["max_trade_eur"] == 40.0 and status["limits"]["max_trades_per_day"] == 2
    r = await api.get("/settings")
    assert "5 aangepast" in r.text  # min_trade_eur equals the environment, so it is not stored

    # Invalid input re-renders the form with the error and changes nothing.
    r = await api.post("/settings", data=form | {"max_trade_eur": "abc"})
    assert r.status_code == 400 and "geen geldig getal" in r.text
    assert (await api.get("/api/trading/status")).json()["limits"]["max_trade_eur"] == 40.0

    # JSON API: markets and interval, including the checkbox-style booleans.
    r = await api.put(
        "/api/settings",
        json={"markets": "sol-eur", "trade_interval_min": 5, "trading_enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["values"]["markets"] == ["SOL-EUR"]
    assert app.state.settings.markets == ["SOL-EUR"] and app.state.engine.markets == ["SOL-EUR"]
    assert app.state.news.coins == ["SOL"] and app.state.settings.trading_enabled is False
    r = await api.post("/api/orders", json={"market": "BTC-EUR", "side": "buy", "amount_quote": 10})
    assert r.status_code == 422  # BTC-EUR is no longer a configured market

    r = await api.put("/api/settings", json={"max_exposure_pct": 500})
    assert r.status_code == 422 and "max_exposure_pct" in r.json()["detail"]

    # Live mode needs the environment safeguards; the portal alone cannot switch it on.
    r = await api.put("/api/settings", json={"trading_mode": "live"})
    assert r.status_code == 422 and "kan nog niet" in r.json()["fields"]["trading_mode"]
    assert r.json()["code"] == "invalid_settings"
    assert (await api.get("/api/settings")).json()["mode"] == "paper"

    # Reset one key, then everything.
    r = await api.delete("/api/settings/markets")
    assert r.json()["values"]["markets"] == ["BTC-EUR", "ETH-EUR"]
    assert (await api.delete("/api/settings/nope")).status_code == 404
    r = await api.delete("/api/settings")
    assert r.json()["values"]["max_trade_eur"] == 100.0 and r.json()["values"]["trading_enabled"]
    assert app.state.engine.limits.max_trade_eur == 100.0

    # Settings survive a restart: they live in the database, not in the process.
    await api.put("/api/settings", json={"max_trades_per_day": 1})
    async with app.router.lifespan_context(app):
        assert app.state.settings.max_trades_per_day == 1
        assert app.state.engine.limits.max_trades_per_day == 1


@respx.mock
async def test_reset_paper_portfolio(api):
    respx.get(f"{BASE}/ticker/price", params={"market": "BTC-EUR"}).mock(
        return_value=Response(200, json={"market": "BTC-EUR", "price": "100"})
    )
    # A manual buy checks the exposure limit, so it needs every market's price.
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(200, json=[{"market": "BTC-EUR", "price": "100"}])
    )
    with SessionLocal() as db:  # orders of earlier tests count for the trades-per-day limit
        db.query(Order).delete()
        db.commit()
    before = (await api.get("/api/balance")).json()["balances"]["EUR"]
    r = await api.post("/api/orders", json={"market": "BTC-EUR", "side": "buy", "amount_quote": 50})
    assert r.status_code == 201, r.text
    assert (await api.get("/api/balance")).json()["balances"]["EUR"] == before - 50

    r = await api.post("/settings", data={"action": "reset_paper"}, follow_redirects=False)
    assert r.status_code == 303
    assert (await api.get("/api/balance")).json()["balances"] == {"EUR": 1000.0}
    r = await api.get("/settings?saved=paper")
    assert "Oefenportefeuille gereset" in r.text


async def test_number_fields_accept_any_decimal(api):
    # Number fields are text fields with a number keyboard: the browser never rewrites '2,5'
    # (a number field in Chromium turns it into 25), and the server reads the decimal comma.
    r = await api.get("/settings")
    assert '<input type="number"' not in r.text
    inputs = re.findall(r"<input [^>]*data-number=[^>]*>", r.text)
    assert len(inputs) > 10
    for tag in inputs:
        assert 'type="text"' in tag and re.search(r'inputmode="(decimal|numeric)"', tag), tag
        assert "step=" not in tag and "pattern=" not in tag, tag
    r = await api.post(
        "/settings",
        data={
            "group": "limits",
            "max_trade_eur": "42",
            "min_trade_eur": "10",
            "max_exposure_pct": "15",
            "daily_loss_limit_pct": "7,5",
            "max_trades_per_day": "6",
            "cooldown_min_after_stop": "240",
            "confirm_loosen": "1",  # earlier tests may have left stricter limits behind
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (await api.get("/api/settings")).json()["values"]["daily_loss_limit_pct"] == 7.5
    await api.delete("/api/settings")
