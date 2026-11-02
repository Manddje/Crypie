"""The bot's status (app/trading/health.py) and the exit loop's own health (UX-06/07/25)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import sessionmaker

from app.bitvavo.client import BitvavoNetworkError
from app.db import Base, Order, Position, Setting, make_engine
from app.trading import risk
from app.trading.health import PAUSED_DETAIL, bot_health, next_utc_midnight, risk_now
from app.web import KILL_SWITCH_NL
from tests.test_trading import FLAT_SERIES, FakeDecider, candles, make_engine_

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def settings(**kw):
    base = {
        "trading_enabled": True,
        "trade_interval_min": 60,
        "exit_check_interval_min": 2,
        "paper_start_eur": 1000.0,
    }
    return SimpleNamespace(**{**base, **kw})


def engine_(session, available=True, mode="hold"):
    eng = make_engine_(
        session,
        {"BTC-EUR": candles(FLAT_SERIES)},
        {"BTC-EUR": 10.0},
        FakeDecider(available=available),
        mode=mode,
    )
    # Running for a while, with a recent exit check and cycle: the normal state.
    eng.started_at = NOW - timedelta(hours=3)
    eng.last_exit_ok = NOW - timedelta(minutes=1)
    eng.last_run = NOW - timedelta(minutes=20)
    return eng


def add_position(session, mode="paper", market="BTC-EUR", amount=5.0, entry=10.0, stop=9.0):
    pos = Position(
        mode=mode,
        market=market,
        amount=amount,
        entry_price=entry,
        entry_quote=amount * entry,
        stop_price=stop,
        high_price=entry,
    )
    session.add(pos)
    session.commit()
    return pos


def health(eng, session, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("scheduled_last", NOW - timedelta(minutes=20))
    return bot_health(eng, session, kw.pop("settings", settings()), **kw)


def test_active_and_healthy(session):
    h = health(engine_(session), session)
    assert (h["level"], h["label"], h["detail"], h["reasons"]) == ("ok", "Actief", None, [])
    assert h["exits_ok"] and h["exit_interval_min"] == 2
    assert h["last_cycle"] == NOW - timedelta(minutes=20)
    # The scheduler plans from its own last start.
    assert h["next_cycle"] == NOW + timedelta(minutes=40)


def test_without_claude_in_hold_mode_the_bot_never_buys(session):
    h = health(engine_(session, available=False, mode="hold"), session)
    assert (h["level"], h["label"]) == ("warn", "Actief · koopt niet")
    assert "geen Claude-sleutel" in h["detail"] and h["buys_blocked"]
    # With "indicators" it still buys on the rules: active, with a note.
    h = health(engine_(session, available=False, mode="indicators"), session)
    assert (h["level"], h["label"]) == ("ok", "Actief")
    assert any("alleen de regels" in r for r in h["reasons"])


def test_empty_claude_budget_blocks_buying_in_hold_mode(session):
    h = health(engine_(session), session, budget={"exhausted": True})
    assert h["label"] == "Actief · koopt niet" and "Claude-budget is op" in h["detail"]


def test_daily_order_maximum_blocks_until_midnight(session):
    eng = engine_(session)
    for _ in range(3):  # LIMITS.max_trades_per_day == 3
        session.add(
            Order(
                mode="paper",
                market="BTC-EUR",
                side="buy",
                order_type="market",
                amount=1.0,
                status="filled",
                created_at=NOW - timedelta(hours=1),
            )
        )
    session.commit()
    figures = risk_now(eng, session, settings(), now=NOW)
    assert figures["orders_today"] == 3 and figures["orders_limit_reached"]
    h = health(eng, session, risk_figures=figures)
    assert h["label"] == "Actief · koopt niet"
    assert "maximum aantal orders per dag" in h["detail"]
    assert "(3 orders vandaag, maximum 3)" in h["detail"]
    assert h["blocked_until"] == datetime(2026, 11, 3, tzinfo=UTC)


def test_daily_loss_limit_blocks_buying(session):
    eng = engine_(session)
    pos = add_position(session, entry=10.0)
    pos.status, pos.closed_at, pos.pnl_eur = "closed", NOW - timedelta(hours=1), -60.0
    session.commit()
    figures = risk_now(eng, session, settings(), now=NOW)
    # The paper wallet starts at 1000 (not created yet): -60 is 6% of it, the limit is 5%.
    assert figures["portfolio_eur"] == 1000 and figures["day_result_pct"] == -6.0
    assert figures["loss_limit_reached"]
    h = health(eng, session, risk_figures=figures)
    assert "dagverlieslimiet van 5% is bereikt" in h["detail"]


def test_kill_switch_is_its_own_state_and_says_what_it_does(session):
    eng = engine_(session)
    risk.set_paused(session, True)
    h = health(eng, session)
    assert (h["level"], h["label"], h["detail"]) == ("paused", "Noodstop actief", PAUSED_DETAIL)
    # The same sentence as next to the kill switch on every page (one source, UX-09/105).
    assert PAUSED_DETAIL == KILL_SWITCH_NL["help"]
    assert "take-profit en verkopen op een verkoopsignaal blijven werken" in PAUSED_DETAIL


def test_trading_disabled(session):
    h = health(engine_(session), session, settings=settings(trading_enabled=False))
    assert (h["level"], h["label"]) == ("off", "Uitgeschakeld")
    assert h["next_cycle"] is None and "stop-loss" in h["detail"]


def test_failing_exit_check_is_an_error_even_when_paused(session):
    eng = engine_(session)
    risk.set_paused(session, True)
    eng.exit_failing_since = NOW - timedelta(minutes=7)
    eng.exit_error = "koersen niet op te halen (time-out)"
    h = health(eng, session)
    assert (h["level"], h["label"]) == ("error", "Storing") and not h["exits_ok"]
    assert "11:53 UTC" in h["detail"] and "time-out" in h["detail"]
    assert "Noodstop actief: kopen gepauzeerd." in h["reasons"]


def test_exit_check_that_stopped_running_is_an_error(session):
    eng = engine_(session)
    eng.last_exit_ok = NOW - timedelta(minutes=30)  # interval 2 min
    h = health(eng, session)
    assert h["level"] == "error" and not h["exits_ok"]
    assert "sinds 11:30 UTC niet meer gelukt" in h["detail"]
    # Right after a start the first run is still to come: no alarm.
    eng.started_at = NOW - timedelta(seconds=30)
    assert health(eng, session)["exits_ok"]
    # Never succeeded since a start long ago: an alarm, also with a long interval.
    eng.started_at, eng.last_exit_ok = NOW - timedelta(hours=3), None
    h = health(eng, session, settings=settings(exit_check_interval_min=60))
    assert not h["exits_ok"] and "sinds de start niet meer gelukt" in h["detail"]


def test_failing_cycle_and_stalled_planner(session):
    eng = engine_(session)
    eng.cycle_error, eng.cycle_error_at = "koersen niet op te halen bij Bitvavo", NOW
    eng.cycle_failing_since = NOW - timedelta(hours=1)
    h = health(eng, session)
    assert h["label"] == "Storing" and "De cyclus mislukt sinds 11:00 UTC" in h["detail"]
    eng.cycle_error = None
    h = health(eng, session, scheduled_last=NOW - timedelta(hours=3))
    assert (h["level"], h["label"]) == ("warn", "Actief") and h["stale"]
    assert "planner draait mogelijk niet" in h["detail"]
    assert h["next_cycle"] == NOW  # overdue: the scheduler starts it within a tick


def test_unprotected_positions_are_an_error(session):
    eng = engine_(session)
    eng.unprotected = [{"id": 1, "mode": "live", "market": "BTC-EUR", "reason": "x"}]
    h = health(eng, session)
    assert h["level"] == "error" and h["unprotected_count"] == 1
    assert "1 positie wordt niet bewaakt" in h["detail"]


def test_risk_now_exposure_from_the_paper_wallet_and_last_prices(session):
    eng = engine_(session)
    session.add(Setting(key="paper_wallet", value='{"EUR": 900.0, "BTC": 5.0}'))
    session.commit()
    add_position(session, amount=5.0, entry=10.0)
    eng.remember_prices({"BTC-EUR": 20.0})
    figures = risk_now(eng, session, settings(), now=NOW)
    assert figures["portfolio_eur"] == 1000.0 and figures["exposure_eur"] == 100.0
    assert figures["exposure_pct"] == 10.0 and figures["max_exposure_pct"] == 50
    assert figures["unrealized_eur"] == 50.0 and figures["day_result_eur"] == 0.0
    assert figures["resets_at"] == next_utc_midnight(NOW)
    # Without a known price the position counts at its entry value.
    eng.last_prices = {}
    figures = risk_now(eng, session, settings(), now=NOW)
    assert figures["exposure_eur"] == 50.0 and figures["portfolio_eur"] == 950.0


# -- the exit loop marks every position unprotected when it cannot check the stops ----------


class DownClient:
    async def ticker_prices(self, markets):
        raise BitvavoNetworkError(TimeoutError("read timed out"))


async def test_failed_price_fetch_marks_all_open_positions_unprotected(session, caplog):
    eng = engine_(session)
    eng.last_exit_ok = None
    a = add_position(session, market="BTC-EUR")
    b = add_position(session, mode="live", market="ETH-EUR")
    eng.client = DownClient()
    with caplog.at_level("ERROR"):
        assert await eng.run_exits(session) == []
    assert [u["id"] for u in eng.unprotected] == [a.id, b.id]
    assert all(
        "stop-loss, trailing stop en take-profit worden niet gecontroleerd" in u["reason"]
        for u in eng.unprotected
    )
    assert eng.exit_failing_since is not None and "Bitvavo reageert niet" in eng.exit_error
    assert eng.last_exit_ok is None
    assert "NOT protected" in caplog.text
    since = eng.exit_failing_since
    await eng.run_exits(session)
    assert eng.exit_failing_since == since  # the first failure stays the start

    # Bitvavo is back: protected again, and the success is remembered.
    eng.client = make_engine_(
        session,
        {"BTC-EUR": candles(FLAT_SERIES)},
        {"BTC-EUR": 10.0, "ETH-EUR": 10.0},
        FakeDecider(),
    ).client
    eng.broker_for_mode = lambda mode: eng.broker if mode == "paper" else None
    await eng.run_exits(session)
    assert eng.exit_failing_since is None and eng.exit_error is None
    assert eng.last_exit_ok is not None
    assert [u["id"] for u in eng.unprotected] == [b.id]  # live cannot trade in this test


async def test_exit_check_runs_the_stops_when_reconciliation_fails(session, monkeypatch):
    eng = engine_(session)
    calls = []

    async def broken(session_):
        raise RuntimeError("database hiccup")

    async def exits(session_):
        calls.append("exits")
        return []

    monkeypatch.setattr(eng, "reconcile", broken)
    monkeypatch.setattr(eng, "run_exits", exits)
    assert await eng.exit_check(session) == [] and calls == ["exits"]


async def test_cycle_failure_is_recorded_and_cleared(session):
    eng = engine_(session)
    good = eng.client
    eng.client = DownClient()
    with pytest.raises(BitvavoNetworkError):
        await eng.run_cycle(session)
    assert "Bitvavo reageert niet" in eng.cycle_error and eng.cycle_failing_since
    eng.client = good
    await eng.run_cycle(session)
    assert eng.cycle_error is None and eng.cycle_failing_since is None


async def test_last_cycle_and_exit_check_survive_a_restart(session):
    eng = engine_(session)
    await eng.run_cycle(session)
    await eng.run_exits(session)
    fresh = make_engine_(
        session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, FakeDecider()
    )
    fresh.load_state(session)
    assert fresh.last_run == eng.last_run and fresh.last_exit_ok == eng.last_exit_ok
    # A stored moment in the future (clock went back) is not believed.
    row = session.get(Setting, "engine_state")
    row.value = row.value.replace(str(eng.last_run.year), "2999", 1)
    session.commit()
    later = make_engine_(
        session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, FakeDecider()
    )
    later.load_state(session)
    assert later.last_run is None


# -- through the app ----------------------------------------------------------------------


@pytest.fixture
async def api():
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_api_status_is_light_and_health_stays_bare(api):
    r = await api.get("/api/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["health"]["level"] in ("ok", "warn", "error", "paused", "off")
    assert {
        "label",
        "detail",
        "reasons",
        "last_cycle",
        "next_cycle",
        "exits_ok",
        "last_exit_ok",
        "exit_interval_min",
    } <= set(body["health"])
    assert {"exposure_pct", "day_result_pct", "orders_today", "max_orders_per_day"} <= set(
        body["risk_now"]
    )
    assert (await api.get("/health")).json() == {"status": "ok"}


async def test_pages_get_health_and_risk_in_their_context(api, monkeypatch):
    from app import main

    seen = {}
    real = main.templates.TemplateResponse

    def spy(request, name, ctx, *args, **kw):
        seen[name] = ctx
        return real(request, name, ctx, *args, **kw)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)

    async def no_prices(markets):
        raise BitvavoNetworkError(TimeoutError("down"))

    monkeypatch.setattr(main.app.state.client, "ticker_prices", no_prices)
    assert (await api.get("/")).status_code == 200
    assert (await api.get("/trading")).status_code == 200
    for name in ("dashboard.html", "trading.html"):
        assert seen[name]["health"]["label"] and "orders_today" in seen[name]["risk_now"]
