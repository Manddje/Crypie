"""Trading plan (/plan) and trade journal (/journal)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.ai.decision_maker import TradeDecision
from app.ai.evaluator import EvaluationResult
from app.db import Decision, Order, Position
from app.trading.journal import (
    JournalFilter,
    journal_entries,
    journal_totals,
    normalize_tags,
    notes_for_claude,
    save_note,
    unlinked_orders,
)
from app.trading.plan import TradingPlan, load_plan, save_plan
from tests.test_trading import (
    BUY_SERIES,
    FakeDecider,
    FakeEvaluator,
    candles,
    make_engine_,
    session,  # noqa: F401 - pytest fixture
)

# -- plan --------------------------------------------------------------------------------


def test_plan_round_trip_and_context(session):  # noqa: F811
    assert load_plan(session).is_empty()
    assert load_plan(session).as_context() is None

    saved = save_plan(session, TradingPlan(goal="Rustig opbouwen", rules="  ", lessons="Kleiner"))
    assert saved.updated_at is not None
    plan = load_plan(session)
    assert plan.goal == "Rustig opbouwen" and plan.rules == "  " and not plan.is_empty()
    ctx = plan.as_context()
    assert ctx["goal"] == "Rustig opbouwen" and ctx["lessons"] == "Kleiner"
    assert "rules" not in ctx and "avoid" not in ctx and "note" in ctx

    save_plan(session, TradingPlan())
    assert load_plan(session).is_empty()


def test_plan_limits_length():
    with pytest.raises(ValueError):
        TradingPlan(goal="x" * 2001)


async def test_claude_receives_plan_and_notes(session):  # noqa: F811
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=50, reasoning_nl="ok")
    )
    evaluator = FakeEvaluator(EvaluationResult(report_nl="rapport"))
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, evaluator=evaluator
    )
    (result,) = await engine.run_cycle(session)
    assert result.executed
    assert decider.contexts[0]["trading_plan"] is None

    save_plan(session, TradingPlan(rules="Niet kopen in het weekend"))
    position = journal_entries(session, "paper")[0].position
    save_note(session, position.id, "Instap was te vroeg", "fout")

    await engine.run_evaluation(session, 7)
    ctx = evaluator.contexts[0]
    assert ctx["trading_plan"]["rules"] == "Niet kopen in het weekend"
    assert ctx["owner_notes"] == [
        {
            "market": "BTC-EUR",
            "opened_at": position.opened_at,
            "pnl_pct": None,
            "exit": None,
            "tags": ["fout"],
            "note": "Instap was te vroeg",
        }
    ]


# -- journal -----------------------------------------------------------------------------


def _trade(db, market, entry, exit_, quote, reason, days_ago, note=None, tags=None, why=None):
    now = datetime.now(UTC)
    opened = now - timedelta(days=days_ago, hours=6)
    closed = now - timedelta(days=days_ago)
    o1 = Order(
        mode="paper",
        market=market,
        side="buy",
        order_type="market",
        amount=quote / entry,
        amount_quote=quote,
        price=entry,
        status="filled",
        reason="claude: x",
        fee_quote=0.25,
        created_at=opened,
    )
    o2 = Order(
        mode="paper",
        market=market,
        side="sell",
        order_type="market",
        amount=quote / entry,
        amount_quote=quote / entry * exit_,
        price=exit_,
        status="filled",
        reason=reason,
        fee_quote=0.25,
        created_at=closed,
    )
    db.add_all([o1, o2])
    db.commit()
    exit_quote = quote / entry * exit_
    p = Position(
        mode="paper",
        market=market,
        status="closed",
        amount=quote / entry,
        entry_price=entry,
        entry_quote=quote,
        opened_at=opened,
        closed_at=closed,
        exit_price=exit_,
        exit_quote=exit_quote,
        pnl_eur=exit_quote - quote,
        pnl_pct=(exit_quote / quote - 1) * 100,
        close_reason=reason,
        open_order_id=o1.id,
        close_order_id=o2.id,
        fees_eur=0.5,
        note=note,
        tags=tags,
    )
    db.add(p)
    db.add(
        Decision(
            mode="paper",
            market=market,
            price=entry,
            signal="buy",
            source="claude",
            model="m",
            action="buy",
            confidence=0.7,
            amount_quote=quote,
            reasoning=why or f"koop {market}",
            executed=True,
            order_id=o1.id,
            news_label="neutraal",
            checks='{"buy": [{"text": "zacht", "left": 1, "right": 2, "ok": false, "soft": true}]}',
            created_at=opened,
        )
    )
    if reason in ("signal", "news_exit"):
        db.add(
            Decision(
                mode="paper",
                market=market,
                price=exit_,
                signal="sell",
                source="claude",
                action="sell",
                reasoning=f"verkoop {market}",
                executed=True,
                order_id=o2.id,
                created_at=closed,
            )
        )
    db.commit()
    return p


def test_journal_joins_decisions_and_computes_totals(session):  # noqa: F811
    win = _trade(session, "BTC-EUR", 100, 110, 100, "signal", 2, note="Goed gedaan", tags="goed")
    _trade(session, "ETH-EUR", 100, 95, 100, "stop_loss", 1)
    _trade(session, "BTC-EUR", 100, 104, 50, "news_exit", 40, why="nieuws positief")
    session.add(
        Position(
            mode="paper",
            market="SOL-EUR",
            status="open",
            amount=1,
            entry_price=50,
            entry_quote=50,
            opened_at=datetime.now(UTC) - timedelta(hours=3),
        )
    )
    session.add(
        Order(
            mode="paper",
            market="XRP-EUR",
            side="buy",
            order_type="market",
            amount_quote=10,
            price=1,
            status="filled",
            reason="manual",
        )
    )
    session.commit()

    entries = journal_entries(session, "paper")
    # Open first, then closed trades newest first
    assert [e.position.market for e in entries] == ["SOL-EUR", "ETH-EUR", "BTC-EUR", "BTC-EUR"]
    assert entries[0].is_open and entries[0].result == "open" and entries[0].exit_nl is None

    first = entries[2]
    assert first.id == win.id and first.result == "win"
    assert first.entry_reasoning == "koop BTC-EUR" and first.exit_reasoning == "verkoop BTC-EUR"
    assert first.entry_source == "claude" and first.entry_news == "neutraal"
    assert first.soft_filters_failed == ["zacht"]
    assert first.exit_nl == "verkoopsignaal van de strategie" and first.duration_nl == "6 uur"
    stop = entries[1]
    assert stop.exit_decision is None and stop.exit_reasoning == "stop_loss"
    assert stop.exit_nl == "stop-loss geraakt"

    totals = journal_totals(entries)
    assert totals["entries"] == 4 and totals["open"] == 1 and totals["closed"] == 3
    assert totals["wins"] == 2 and totals["losses"] == 1
    assert totals["win_rate_pct"] == pytest.approx(66.7)
    assert totals["total_pnl_eur"] == pytest.approx(7.0)  # +10 -5 +2
    assert totals["total_fees_eur"] == pytest.approx(1.5)
    assert totals["return_pct"] == pytest.approx(2.8)  # 7 on 250 invested
    assert totals["profit_factor"] == pytest.approx(2.4)  # 12 / 5
    assert totals["avg_duration_min"] == 360 and totals["with_note"] == 1
    assert totals["exits"] == {"signal": 1, "stop_loss": 1, "news_exit": 1}
    assert [(m["market"], m["trades"], m["pnl_eur"]) for m in totals["per_market"]] == [
        ("BTC-EUR", 2, 12.0),
        ("ETH-EUR", 1, -5.0),
    ]

    # Filters
    assert len(journal_entries(session, "paper", JournalFilter(market="BTC-EUR"))) == 2
    assert len(journal_entries(session, "paper", JournalFilter(result="loss"))) == 1
    assert len(journal_entries(session, "paper", JournalFilter(status="open"))) == 1
    assert len(journal_entries(session, "paper", JournalFilter(reason="news_exit"))) == 1
    assert len(journal_entries(session, "paper", JournalFilter(days=7))) == 3  # one is 40 days old
    assert len(journal_entries(session, "paper", JournalFilter(tag="Goed"))) == 1
    found = journal_entries(session, "paper", JournalFilter(query="NIEUWS"))
    assert [e.position.entry_quote for e in found] == [50]
    assert journal_entries(session, "live") == []

    # Orders that belong to no position are listed separately
    assert [o.market for o in unlinked_orders(session, "paper")] == ["XRP-EUR"]

    # Notes for the evaluator: only trades with a note or tag
    notes = notes_for_claude(entries)
    assert len(notes) == 1 and notes[0]["note"] == "Goed gedaan" and notes[0]["tags"] == ["goed"]


def test_notes_and_tags(session):  # noqa: F811
    assert normalize_tags("Fout, Nieuws FOMO; fout, ") == "fout,nieuws-fomo"
    assert normalize_tags(None) == ""
    p = _trade(session, "BTC-EUR", 100, 101, 100, "signal", 1)
    saved = save_note(session, p.id, "  Te vroeg ingestapt  ", "Fout, Les")
    assert saved.note == "Te vroeg ingestapt" and saved.tags == "fout,les"
    assert saved.note_updated_at is not None and saved.tag_list() == ["fout", "les"]
    cleared = save_note(session, p.id, "", "")
    assert cleared.note is None and cleared.tags is None and cleared.note_updated_at is None
    with pytest.raises(LookupError):
        save_note(session, 999, "x", "")
    with pytest.raises(ValueError):
        save_note(session, p.id, "x" * 4001, "")


# -- pages -------------------------------------------------------------------------------


@pytest.fixture
async def api():
    from sqlalchemy import delete

    from app.db import Setting
    from app.main import SessionLocal, app
    from app.trading.plan import PLAN_KEY

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    # The in-memory database outlives the test; leave it as the other API tests expect it.
    with SessionLocal() as s:
        for model in (Position, Decision, Order):
            s.execute(delete(model))
        s.execute(delete(Setting).where(Setting.key == PLAN_KEY))
        s.commit()


async def test_plan_page_and_api(api):
    r = await api.get("/plan")
    assert r.status_code == 200
    assert "Tradingplan" in r.text and "Harde limieten" in r.text and "rsi14 lager dan 35" in r.text

    r = await api.post("/plan", data={"goal": "Rustig opbouwen", "rules": "Geen weekend"})
    assert r.status_code == 303 and r.headers["location"] == "/plan?saved=1"
    r = await api.get("/plan?saved=1")
    assert "Rustig opbouwen" in r.text and "Tradingplan opgeslagen" in r.text

    r = await api.get("/api/plan")
    assert r.json()["goal"] == "Rustig opbouwen" and r.json()["avoid"] == ""
    r = await api.put("/api/plan", json={"lessons": "Kleiner op FARTCOIN"})
    assert r.status_code == 200 and r.json()["lessons"] == "Kleiner op FARTCOIN"
    assert r.json()["goal"] == ""  # a PUT replaces the whole plan

    r = await api.post("/plan", data={"goal": "x" * 2001})
    assert r.status_code == 400 and "Doel en horizon" in r.text


async def test_journal_page_and_notes(api):
    from app.db import SessionLocal

    r = await api.get("/journal")
    assert r.status_code == 200 and "Nog geen trades" in r.text

    with SessionLocal() as s:
        p = _trade(s, "BTC-EUR", 100, 110, 100, "signal", 1, why="RSI draait omhoog")
        pid = p.id
    r = await api.get("/journal")
    assert "RSI draait omhoog" in r.text and "verkoop BTC-EUR" in r.text and "Winst" in r.text

    r = await api.post(
        f"/journal/{pid}/note",
        data={"note": "Goede trade", "tags": "Goed, les", "back": "/journal?market=BTC-EUR"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/journal?market=BTC-EUR&saved={pid}#trade-{pid}"
    r = await api.get(f"/journal?market=BTC-EUR&saved={pid}")
    assert (
        "Goede trade" in r.text
        and "Notitie opgeslagen" in r.text
        and 'href="/journal?tag=les"' in r.text
    )
    assert 'name="back" value="/journal?market=BTC-EUR"' in r.text

    r = await api.get("/api/journal?result=win")
    body = r.json()
    assert body["mode"] == "paper" and body["totals"]["wins"] == 1
    assert body["entries"][0]["tags"] == ["goed", "les"]
    assert body["entries"][0]["entry_reasoning"] == "RSI draait omhoog"
    r = await api.get("/api/journal?result=loss")
    assert r.json()["entries"] == [] and r.json()["totals"]["win_rate_pct"] is None

    r = await api.put(f"/api/journal/{pid}/note", json={"note": "", "tags": ""})
    assert r.status_code == 200 and r.json()["note"] is None
    r = await api.put("/api/journal/999/note", json={"note": "x"})
    assert r.status_code == 404
    r = await api.post("/journal/999/note", data={"note": "x"})
    assert r.status_code == 404
