"""Step-up re-authentication for sensitive settings and live manual orders (audit F21)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import settings_store as cfg
from app.auth import routes
from app.db import AuthThrottle, SessionLocal
from app.main import app
from app.trading import risk
from tests.test_auth import PASSWORD, _setup
from tests.test_security_auth import web  # noqa: F401  (fixture)

WRONG = "niet-het-wachtwoord"


@pytest.fixture
async def owner(web):  # noqa: F811
    """Logged in with password and authenticator; stored portal settings are forgotten after."""
    await _setup(web)
    try:
        yield web
    finally:
        with SessionLocal() as db:
            cfg.reset_overrides(db)


def _failures() -> int:
    with SessionLocal() as db:
        return sum(row.failures for row in db.query(AuthThrottle).all())


def test_sensitive_keys_cover_mode_markets_feeds_budget_and_every_limit():
    expected = {
        "trading_mode",
        "markets",
        "claude_unavailable_mode",
        "risk_off_enabled",
        "news_rss_feeds",
        "claude_monthly_budget_usd",
    }
    assert expected <= cfg.SENSITIVE_KEYS
    assert set(cfg.keys_in_group("limits")) <= cfg.SENSITIVE_KEYS
    assert "trade_interval_min" not in cfg.SENSITIVE_KEYS
    assert "claude_model_news" not in cfg.SENSITIVE_KEYS


# -- /settings form -----------------------------------------------------------------------------


async def test_settings_form_asks_the_password_for_a_sensitive_change(owner):
    page = await owner.get("/settings")
    assert page.text.count('name="current_password"') == len(cfg.GROUPS) + 1  # + paper reset
    assert "Huidig wachtwoord" in page.text
    # The help names every sensitive part, also trading on/off and the exit check (F21).
    assert "automatisch handelen aan/uit" in page.text and "de exit-controle" in page.text

    form = {"group": "limits", "action": "save", "max_trade_eur": "40"}
    r = await owner.post("/settings", data=form)
    assert r.status_code == 403 and "Vul je huidige wachtwoord in" in r.text
    assert 'value="40"' in r.text  # the submitted value is kept in the form
    assert app.state.settings.max_trade_eur == 100.0
    assert _failures() == 0  # a missing password is not a guess

    r = await owner.post("/settings", data=form | {"current_password": WRONG})
    assert r.status_code == 403 and "Het wachtwoord klopt niet" in r.text
    assert 'value="40"' in r.text and WRONG not in r.text
    assert app.state.settings.max_trade_eur == 100.0
    assert _failures() == 1

    r = await owner.post(
        "/settings", data=form | {"current_password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?saved=limits")
    assert app.state.settings.max_trade_eur == 40.0
    assert app.state.engine.limits.max_trade_eur == 40.0
    assert _failures() == 0  # the right password clears the address's failures


async def test_settings_form_without_sensitive_change_needs_no_password(owner):
    # Re-sending the current value of a sensitive key is not a change either.
    budget = f"{app.state.settings.claude_monthly_budget_usd:g}"
    form = {
        "group": "claude",
        "evaluation_interval_hours": "12",
        "claude_monthly_budget_usd": budget,
    }
    r = await owner.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert app.state.settings.evaluation_interval_hours == 12


async def test_emergency_pause_needs_no_password(owner):
    """Turning trading_enabled off in the settings needs the password; the pause button on
    /trading does not, so the bot can always be stopped quickly."""
    r = await owner.post("/trading/action", data={"action": "pause"}, follow_redirects=False)
    assert r.status_code == 303
    try:
        with SessionLocal() as db:
            assert risk.is_paused(db)
    finally:
        with SessionLocal() as db:
            risk.set_paused(db, False)


async def test_validation_errors_come_before_the_password(owner):
    r = await owner.post("/settings", data={"group": "limits", "max_trade_eur": "abc"})
    assert r.status_code == 400 and "geen geldig getal" in r.text


async def test_settings_form_reset_of_a_sensitive_group_needs_the_password(owner):
    r = await owner.put(
        "/api/settings", json={"max_trades_per_day": 3, "current_password": PASSWORD}
    )
    assert r.status_code == 200
    r = await owner.post("/settings", data={"group": "limits", "action": "reset"})
    assert r.status_code == 403 and "Vul je huidige wachtwoord in" in r.text
    assert app.state.settings.max_trades_per_day == 3
    r = await owner.post(
        "/settings",
        data={"group": "limits", "action": "reset", "current_password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert app.state.settings.max_trades_per_day != 3
    # Nothing sensitive stored in a group: resetting it needs no password.
    r = await owner.post(
        "/settings", data={"group": "limits", "action": "reset"}, follow_redirects=False
    )
    assert r.status_code == 303


async def test_paper_reset_needs_the_password(owner):
    r = await owner.post("/settings", data={"group": "trading", "action": "reset_paper"})
    assert r.status_code == 403 and "Vul je huidige wachtwoord in" in r.text
    r = await owner.post(
        "/settings",
        data={"group": "trading", "action": "reset_paper", "current_password": WRONG},
    )
    assert r.status_code == 403 and "Het wachtwoord klopt niet" in r.text
    r = await owner.post(
        "/settings",
        data={"group": "trading", "action": "reset_paper", "current_password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=paper"


# -- JSON API -----------------------------------------------------------------------------------


async def test_api_put_needs_current_password_for_sensitive_keys(owner):
    r = await owner.put("/api/settings", json={"markets": "SOL-EUR"})
    assert r.status_code == 403 and "wachtwoord" in r.json()["detail"]
    r = await owner.put("/api/settings", json={"markets": "SOL-EUR", "current_password": WRONG})
    assert r.status_code == 403 and r.json()["detail"] == "Het wachtwoord klopt niet."
    assert app.state.settings.markets == ["BTC-EUR", "ETH-EUR"]

    feeds = {"news_rss_feeds": ["https://feeds.example.com/rss"]}
    for body in (
        {"trading_mode": "paper", "risk_off_enabled": False},
        {"claude_unavailable_mode": "indicators"},
        {"claude_monthly_budget_usd": 50},
        {"cooldown_min_after_stop": 0},
        feeds,
    ):
        r = await owner.put("/api/settings", json=body)
        assert r.status_code == 403, body

    r = await owner.put("/api/settings", json={"markets": "SOL-EUR", "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["values"]["markets"] == ["SOL-EUR"]
    assert "current_password" not in r.json()["values"]
    # The header works too.
    r = await owner.put("/api/settings", json=feeds, headers={"X-Current-Password": PASSWORD})
    assert r.status_code == 200, r.text


async def test_api_put_of_harmless_settings_needs_no_password(owner):
    r = await owner.put("/api/settings", json={"trade_interval_min": 5, "news_batch_size": 10})
    assert r.status_code == 200, r.text
    r = await owner.put("/api/settings", json={"current_password": 123})
    assert r.status_code == 422


async def test_api_reset_needs_the_password_when_a_sensitive_value_is_stored(owner):
    await owner.put("/api/settings", json={"max_exposure_pct": 40, "current_password": PASSWORD})
    await owner.put("/api/settings", json={"trade_interval_min": 5})

    # Resetting a harmless key, or one that is not stored, needs nothing.
    assert (await owner.delete("/api/settings/trade_interval_min")).status_code == 200
    assert (await owner.delete("/api/settings/markets")).status_code == 200

    r = await owner.delete("/api/settings/max_exposure_pct")
    assert r.status_code == 403
    r = await owner.delete("/api/settings", headers={"X-Current-Password": WRONG})
    assert r.status_code == 403
    assert app.state.settings.max_exposure_pct == 40.0

    r = await owner.request(
        "DELETE", "/api/settings/max_exposure_pct", json={"current_password": PASSWORD}
    )
    assert r.status_code == 200
    assert app.state.settings.max_exposure_pct != 40.0

    await owner.put("/api/settings", json={"max_exposure_pct": 40, "current_password": PASSWORD})
    r = await owner.delete("/api/settings", headers={"X-Current-Password": PASSWORD})
    assert r.status_code == 200
    assert app.state.settings.max_exposure_pct != 40.0


async def test_wrong_step_up_passwords_are_throttled(owner):
    for _ in range(app.state.auth.throttle.max_failures):
        r = await owner.put("/api/settings", json={"max_trade_eur": 50, "current_password": WRONG})
        assert r.status_code == 403
    r = await owner.put("/api/settings", json={"max_trade_eur": 50, "current_password": PASSWORD})
    assert r.status_code == 429 and "Te veel mislukte pogingen" in r.json()["detail"]
    r = await owner.post(
        "/settings", data={"group": "limits", "max_trade_eur": "50", "current_password": PASSWORD}
    )
    assert r.status_code == 429 and "Te veel mislukte pogingen" in r.text
    assert app.state.settings.max_trade_eur == 100.0
    with SessionLocal() as db:
        db.query(AuthThrottle).delete()
        db.commit()


async def test_step_up_answers_429_when_all_hash_slots_are_busy(owner):
    taken = 0
    while routes._HASH_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        r = await owner.put("/api/settings", json={"max_trade_eur": 50, "current_password": WRONG})
        assert r.status_code == 429 and "druk" in r.json()["detail"]
    finally:
        for _ in range(taken):
            routes._HASH_SLOTS.release()
    assert _failures() == 0


# -- manual orders while live -------------------------------------------------------------------


@pytest.fixture
def live(owner, monkeypatch):
    """Pretend the broker trades live; record manual orders instead of placing them."""
    placed: list[tuple] = []

    class LiveStub:
        mode = "live"

        async def balances(self, session):
            return {"EUR": 1000.0}

    async def manual_order(session, market, side, amount_quote, reason=None):
        placed.append((market, side, amount_quote))
        return SimpleNamespace(
            id=1,
            created_at=datetime.now(UTC),
            market=market,
            side=side,
            status="filled",
            price=100.0,
            amount=0.1,
            amount_quote=amount_quote,
            fee_quote=0.0,
            mode="live",
            reason=reason,
        )

    async def no_prices(markets):
        return {}

    engine = app.state.engine
    monkeypatch.setattr(engine, "broker", LiveStub())
    monkeypatch.setattr(engine, "manual_order", manual_order)
    monkeypatch.setattr(app.state.client, "ticker_prices", no_prices)
    return placed


async def test_live_manual_orders_need_the_password(owner, live):
    order = {"market": "BTC-EUR", "side": "buy", "amount_quote": 10}
    r = await owner.post("/api/orders", json=order)
    assert r.status_code == 403
    r = await owner.post("/api/orders", json=order | {"current_password": WRONG})
    assert r.status_code == 403
    assert live == []
    r = await owner.post("/api/orders", json=order | {"current_password": PASSWORD})
    assert r.status_code == 201 and live == [("BTC-EUR", "buy", 10.0)]
    r = await owner.post("/api/orders", json=order, headers={"X-Current-Password": PASSWORD})
    assert r.status_code == 201

    page = await owner.get("/")
    assert 'name="current_password"' in page.text
    form = {"market": "BTC-EUR", "side": "sell", "amount_quote": "10"}
    r = await owner.post("/orders", data=form)
    assert r.status_code == 403 and "Order niet geplaatst" in r.text
    r = await owner.post("/orders", data=form | {"current_password": WRONG})
    assert r.status_code == 403 and "Het wachtwoord klopt niet" in r.text
    assert len(live) == 2
    r = await owner.post(
        "/orders", data=form | {"current_password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303 and live[-1] == ("BTC-EUR", "sell", 10.0)


async def test_paper_manual_orders_need_no_password(owner, monkeypatch):
    placed = []

    async def manual_order(session, market, side, amount_quote, reason=None):
        placed.append(market)
        return SimpleNamespace(
            id=1,
            created_at=datetime.now(UTC),
            market=market,
            side=side,
            status="filled",
            price=1.0,
            amount=1.0,
            amount_quote=10.0,
            fee_quote=0.0,
            mode="paper",
            reason=reason,
        )

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    assert app.state.engine.mode == "paper"
    assert 'name="current_password"' not in (await owner.get("/")).text
    r = await owner.post(
        "/api/orders", json={"market": "BTC-EUR", "side": "buy", "amount_quote": 10}
    )
    assert r.status_code == 201 and placed == ["BTC-EUR"]


# -- without the login there is no password to ask for ------------------------------------------


async def test_without_login_there_is_no_step_up():
    from httpx import ASGITransport, AsyncClient

    async with app.router.lifespan_context(app):
        assert app.state.auth is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert 'name="current_password"' not in (await c.get("/settings")).text
            r = await c.put("/api/settings", json={"max_trade_eur": 40})
            assert r.status_code == 200
            assert (await c.delete("/api/settings")).status_code == 200
