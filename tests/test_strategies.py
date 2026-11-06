"""Several strategies, each with its own markets and its own budget."""

import json
import math

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.ai.decision_maker import TradeDecision
from app.bitvavo.broker import PAPER_WALLET_KEY
from app.db import (
    Decision,
    Evaluation,
    Order,
    Position,
    SessionLocal,
    Setting,
    StrategyRecord,
    init_db,
)
from app.flash import flashes_from_response
from app.main import app
from app.strategy.model import DEFAULT_STRATEGY, Strategy
from app.strategy.store import (
    PREVIOUS_KEY,
    STRATEGY_KEY,
    StrategySlot,
    add_slot,
    assign_markets,
    delete_slot,
    ensure_strategies,
    exit_slot_for,
    load_previous,
    load_slots,
    load_strategy,
    market_problems,
    parse_budget,
    parse_markets,
    resolved_markets,
    save_slot,
    save_strategy,
    slot_version,
)
from app.trading import risk
from app.trading.engine import (
    NO_STRATEGY_REASON,
    STRATEGY_BUDGET_BLOCK,
    ManualOrderError,
    open_position,
)
from tests.test_trading import (  # noqa: F401 - the engine helpers and the session fixture
    BUY_SERIES,
    FLAT_SERIES,
    FakeDecider,
    candles,
    cross_strategy,
    make_engine_,
    session,
)

HTML = {"accept": "text/html,application/xhtml+xml"}
MARKETS = ["BTC-EUR", "ETH-EUR"]


def _buyer(amount: float = 80) -> FakeDecider:
    return FakeDecider(
        TradeDecision(action="buy", confidence=0.9, amount_quote=amount, reasoning_nl="Koop.")
    )


# -- store ------------------------------------------------------------------------------------


def test_an_older_installation_keeps_its_one_strategy(session):  # noqa: F811
    """The strategy in the settings table moves to the strategies table as the first strategy:
    every market, no budget of its own, its previous rules kept."""
    custom = cross_strategy(name="Van vroeger")
    session.add(Setting(key=STRATEGY_KEY, value=custom.model_dump_json()))
    session.add(
        Setting(
            key=PREVIOUS_KEY,
            value=json.dumps(
                {
                    "strategy": DEFAULT_STRATEGY.model_dump(mode="json"),
                    "replaced_at": "2026-01-01T00:00:00+00:00",
                }
            ),
        )
    )
    session.commit()
    (slot,) = load_slots(session)
    assert slot.strategy.name == "Van vroeger" and slot.catch_all and slot.budget_eur == 0
    assert slot.enabled and slot.previous is not None
    assert slot.previous.name == DEFAULT_STRATEGY.name
    assert load_strategy(session).name == "Van vroeger"
    previous, when = load_previous(session)
    assert previous.name == DEFAULT_STRATEGY.name and when is not None
    # The one-strategy interface keeps working on the first strategy.
    save_strategy(session, DEFAULT_STRATEGY)
    assert load_strategy(session) == DEFAULT_STRATEGY
    assert load_previous(session)[0].name == "Van vroeger"


def test_without_anything_stored_the_default_strategy_is_the_first(session):  # noqa: F811
    assert session.scalar(select(StrategyRecord.id)) is None
    (slot,) = load_slots(session)
    assert slot.strategy == DEFAULT_STRATEGY and slot.id >= 1
    ensure_strategies(session)
    assert len(load_slots(session)) == 1  # idempotent


def test_markets_go_to_the_strategy_that_names_them_and_the_rest_to_the_catch_all(
    session,  # noqa: F811
):
    first = load_slots(session)[0]
    eth = add_slot(session, cross_strategy(name="ETH"), markets=["ETH-EUR"], budget_eur=50)
    plan = assign_markets(load_slots(session), ["BTC-EUR", "ETH-EUR", "SOL-EUR"])
    assert plan["ETH-EUR"].id == eth.id and plan["BTC-EUR"].id == first.id
    assert plan["SOL-EUR"].id == first.id
    assert eth.active_markets is None  # filled by assign_markets on the loaded objects
    slots = load_slots(session)
    assign_markets(slots, MARKETS)
    assert [s.active_markets for s in slots] == [["BTC-EUR"], ["ETH-EUR"]]
    # Switched off: its market goes to the catch-all.
    save_slot(session, eth.id, enabled=False)
    slots = load_slots(session)
    plan = assign_markets(slots, MARKETS)
    assert set(plan) == {"BTC-EUR", "ETH-EUR"} and plan["ETH-EUR"].id == first.id
    # Without a catch-all, an unnamed market has no strategy at all.
    save_slot(session, first.id, markets=["BTC-EUR"])
    plan = assign_markets(load_slots(session), MARKETS)
    assert set(plan) == {"BTC-EUR"}
    assert resolved_markets(load_slots(session)[1], load_slots(session), MARKETS) == ["ETH-EUR"]


def test_market_problems_overlap_unknown_and_second_catch_all(session):  # noqa: F811
    slots = load_slots(session)
    first = slots[0]
    assert market_problems(slots, ["XRP-EUR"], slot_id=None, enabled=True, all_markets=MARKETS)
    # A second strategy without markets would compete with the first one for the rest.
    (problem,) = market_problems(slots, [], slot_id=None, enabled=True, all_markets=MARKETS)
    assert "alle overige markten" in problem
    assert market_problems(slots, [], slot_id=None, enabled=False, all_markets=MARKETS) == []
    eth = add_slot(session, cross_strategy(name="ETH"), markets=["ETH-EUR"])
    slots = load_slots(session)
    (problem,) = market_problems(
        slots, ["ETH-EUR"], slot_id=first.id, enabled=True, all_markets=MARKETS
    )
    assert "ETH-EUR hoort al bij strategie 'ETH'" in problem
    # Its own markets are no conflict with itself.
    assert not market_problems(
        slots, ["ETH-EUR"], slot_id=eth.id, enabled=True, all_markets=MARKETS
    )


def test_parse_budget_and_markets():
    assert parse_budget("") == 0 and parse_budget("250,50") == 250.5 and parse_budget(0) == 0
    for bad in ("abc", "-5", "2", "nan", float("inf")):
        with pytest.raises(ValueError):
            parse_budget(bad)
    assert parse_markets(" btc-eur, ETH-EUR ,btc-eur") == ["BTC-EUR", "ETH-EUR"]
    assert parse_markets(["sol-eur"]) == ["SOL-EUR"] and parse_markets(None) == []
    with pytest.raises(ValueError):
        parse_markets("BTCEUR")


def test_save_keeps_previous_rules_and_the_last_strategy_cannot_go(session):  # noqa: F811
    first = load_slots(session)[0]
    before = slot_version(session, first.id)
    save_slot(session, first.id, cross_strategy(name="Nieuw"), budget_eur=100, markets=["BTC-EUR"])
    slot = load_slots(session)[0]
    assert slot.name == "Nieuw" and slot.budget_eur == 100 and slot.markets == ["BTC-EUR"]
    assert slot.previous == DEFAULT_STRATEGY and slot_version(session, first.id) != before
    # Only the switch: the rules (and the previous rules) stay.
    save_slot(session, first.id, enabled=False)
    slot = load_slots(session)[0]
    assert not slot.enabled and slot.previous == DEFAULT_STRATEGY
    with pytest.raises(ValueError):
        delete_slot(session, first.id)
    second = add_slot(session, DEFAULT_STRATEGY, markets=["ETH-EUR"])
    delete_slot(session, first.id)
    assert [s.id for s in load_slots(session)] == [second.id]


def test_exit_slot_for_prefers_the_market_owner_then_the_opener():
    a = StrategySlot(id=1, strategy=cross_strategy(name="A"), markets=["BTC-EUR"])
    b = StrategySlot(id=2, strategy=cross_strategy(name="B"), markets=["ETH-EUR"], enabled=False)
    plan = assign_markets([a, b], MARKETS)
    assert exit_slot_for("BTC-EUR", 2, plan, [a, b]) is a  # the market's strategy now
    assert exit_slot_for("ETH-EUR", 2, plan, [a, b]) is b  # switched off, but it opened it
    assert exit_slot_for("ETH-EUR", 9, plan, [a, b]) is a  # gone: the first strategy
    assert exit_slot_for("SOL-EUR", None, {}, []) is None


# -- engine -----------------------------------------------------------------------------------


async def test_each_market_trades_with_its_own_strategy_and_budget(session):  # noqa: F811
    """BTC follows strategy A (budget 50), ETH the catch-all B: both buy on their own rules,
    A's buy is capped on its budget and every position and decision names its strategy."""
    decider = _buyer(80)
    series = {"BTC-EUR": candles(BUY_SERIES), "ETH-EUR": candles(BUY_SERIES)}
    engine = make_engine_(session, series, {"BTC-EUR": 12.0, "ETH-EUR": 12.0}, decider)
    b = load_slots(session)[0]
    a = add_slot(
        session, cross_strategy(name="A", stop_loss_pct=20), markets=["BTC-EUR"], budget_eur=50
    )
    results = {r.market: r for r in await engine.run_cycle(session)}
    assert results["BTC-EUR"].executed and results["ETH-EUR"].executed
    btc = open_position(session, "paper", "BTC-EUR")
    eth = open_position(session, "paper", "ETH-EUR")
    assert btc.strategy_id == a.id and eth.strategy_id == b.id
    assert btc.entry_quote == pytest.approx(50.0)  # A's budget, not Claude's 80
    assert eth.entry_quote == pytest.approx(80.0)  # B has no budget of its own
    assert btc.stop_price == pytest.approx(12.0 * 0.8)  # A's stop-loss (20%), not B's (10%)
    assert eth.stop_price == pytest.approx(12.0 * 0.9)
    by_market = {d.market: d for d in session.scalars(select(Decision))}
    assert by_market["BTC-EUR"].strategy_id == a.id and by_market["ETH-EUR"].strategy_id == b.id
    # Claude saw the budget of A and no budget for B.
    ctx = {c["market"]: c for c in decider.contexts}
    assert ctx["BTC-EUR"]["strategy_budget"]["available_eur"] == 50.0
    assert ctx["BTC-EUR"]["limits"]["strategy_available_eur"] == 50.0
    assert ctx["ETH-EUR"]["strategy_budget"]["budget_eur"] is None
    assert "strategy_available_eur" not in ctx["ETH-EUR"]["limits"]
    # What the pages and the API show per strategy.
    rows = {r["slot"].id: r for r in engine.strategy_slots(session)}
    assert rows[a.id]["invested_eur"] == 50.0 and rows[a.id]["room_eur"] == 0.0
    assert rows[a.id]["active_markets"] == ["BTC-EUR"] and rows[a.id]["open_positions"] == 1
    assert rows[b.id]["room_eur"] is None and rows[b.id]["active_markets"] == ["ETH-EUR"]


async def test_a_spent_budget_blocks_the_buy_without_asking_claude_twice(session):  # noqa: F811
    decider = _buyer(80)
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    first = load_slots(session)[0]
    save_slot(session, first.id, budget_eur=50)
    # A position this strategy opened earlier in a market the bot no longer tracks still
    # ties up its budget.
    session.add(
        Position(
            mode="paper",
            market="ETH-EUR",
            amount=1,
            entry_price=45,
            entry_quote=45,
            stop_price=1,
            strategy_id=first.id,
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and not result.executed
    assert STRATEGY_BUDGET_BLOCK in result.reason and "€ 45.00 van € 50.00" in result.reason
    assert open_position(session, "paper", "BTC-EUR") is None
    (d,) = session.scalars(select(Decision).where(Decision.market == "BTC-EUR"))
    assert d.blocked_by.startswith(STRATEGY_BUDGET_BLOCK)
    assert risk.budget_room(session, "paper", ["BTC-EUR", "ETH-EUR"], 50) == (45.0, 5.0)
    assert risk.budget_room(session, "paper", ["BTC-EUR"], 50, first.id) == (45.0, 5.0)
    assert risk.budget_room(session, "paper", ["BTC-EUR"], 50) == (0.0, 50.0)
    assert risk.budget_room(session, "paper", ["BTC-EUR"], 0)[1] == math.inf


async def test_a_market_without_a_strategy_is_not_traded_but_still_guarded(
    session,  # noqa: F811
):
    decider = _buyer(80)
    series = {"BTC-EUR": candles(BUY_SERIES), "ETH-EUR": candles(BUY_SERIES)}
    engine = make_engine_(session, series, {"BTC-EUR": 12.0, "ETH-EUR": 1.0}, decider)
    first = load_slots(session)[0]
    save_slot(session, first.id, markets=["BTC-EUR"])
    session.add(
        Position(
            mode="paper", market="ETH-EUR", amount=1, entry_price=2, entry_quote=2, stop_price=1.5
        )
    )
    session.add(Setting(key=PAPER_WALLET_KEY, value=json.dumps({"EUR": 1000, "ETH": 1})))
    session.commit()
    results = {r.market: r for r in await engine.run_cycle(session)}
    assert results["BTC-EUR"].executed
    assert results["ETH-EUR"].reason == NO_STRATEGY_REASON and not results["ETH-EUR"].executed
    assert [c["market"] for c in decider.contexts] == ["BTC-EUR"]
    assert session.scalar(select(Decision).where(Decision.market == "ETH-EUR")) is None
    # The exit loop still sells the ETH position on its stop (with the first strategy's rules).
    (sold,) = await engine.run_exits(session)
    assert sold.market == "ETH-EUR" and sold.executed and sold.reason == "stop_loss"
    with pytest.raises(ValueError, match="geen strategie"):
        await engine.run_consult(session, "ETH-EUR")


async def test_an_invalid_strategy_only_blocks_its_own_markets(session):  # noqa: F811
    decider = _buyer(80)
    series = {"BTC-EUR": candles(BUY_SERIES), "ETH-EUR": candles(BUY_SERIES)}
    engine = make_engine_(session, series, {"BTC-EUR": 12.0, "ETH-EUR": 12.0}, decider)
    broken = add_slot(session, cross_strategy(name="Kapot"), markets=["ETH-EUR"])
    session.get(StrategyRecord, broken.id).config = "{not json"
    session.commit()
    results = {r.market: r for r in await engine.run_cycle(session)}
    assert results["BTC-EUR"].executed and not results["ETH-EUR"].executed
    assert engine.strategy_invalid is True
    # Only BTC (the valid strategy) went to Claude; ETH followed the default rules for its
    # exits only and opened nothing.
    assert [c["market"] for c in decider.contexts] == ["BTC-EUR"]
    assert open_position(session, "paper", "ETH-EUR") is None


async def test_a_manual_buy_respects_the_strategy_budget_of_its_market(session):  # noqa: F811
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, _buyer())
    first = load_slots(session)[0]
    save_slot(session, first.id, budget_eur=30)
    with pytest.raises(ManualOrderError, match="budget van strategie"):
        await engine.manual_order(session, "BTC-EUR", "buy", 40)
    order = await engine.manual_order(session, "BTC-EUR", "buy", 25)
    assert order.status == "filled"
    assert open_position(session, "paper", "BTC-EUR").strategy_id == first.id
    with pytest.raises(ManualOrderError, match="open positie"):
        await engine.manual_order(session, "BTC-EUR", "buy", 5)


async def test_the_evaluation_lists_every_strategy_and_targets_the_one_claude_names(
    session,  # noqa: F811
):
    from app.ai.evaluator import EvaluationResult
    from tests.test_trading import FakeEvaluator

    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, _buyer())
    second = add_slot(session, cross_strategy(name="Tweede"), markets=["BTC-EUR"], budget_eur=20)
    proposal = cross_strategy(name="Tweede, strakker", stop_loss_pct=5)
    engine.evaluator = FakeEvaluator(
        EvaluationResult(
            report_nl="r", proposed_strategy=proposal, proposed_for_strategy_id=second.id
        )
    )
    ev = await engine.run_evaluation(session)
    (ctx,) = engine.evaluator.contexts
    assert [s["strategy_id"] for s in ctx["strategies"]] == [load_slots(session)[0].id, second.id]
    assert ctx["strategies"][1]["budget_eur"] == 20
    assert ctx["strategies"][1]["markets"] == ["BTC-EUR"] and ctx["strategies"][0]["markets"] == []
    assert ev.strategy_id == second.id


# -- API and pages ----------------------------------------------------------------------------


def _clean():
    with SessionLocal() as s:
        for table in (Decision, Order, Position, Evaluation, StrategyRecord):
            s.execute(delete(table))
        s.execute(delete(Setting).where(Setting.key.in_((STRATEGY_KEY, PREVIOUS_KEY))))
        s.commit()
        risk.set_paused(s, False)


@pytest.fixture
async def api():
    init_db()
    _clean()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    _clean()


async def test_strategies_api_crud(api):
    (first,) = (await api.get("/api/strategies")).json()
    assert first["name"] == DEFAULT_STRATEGY.name and first["markets"] == []
    assert first["active_markets"] == MARKETS and first["budget_eur"] == 0
    assert first["available_eur"] is None and first["enabled"] and first["valid"]

    body = {
        "strategy": cross_strategy(name="ETH-dip").model_dump(mode="json"),
        "markets": ["eth-eur"],
        "budget_eur": 150,
    }
    r = await api.post("/api/strategies", json=body)
    assert r.status_code == 201, r.text
    new = r.json()
    assert r.headers["location"] == f"/api/strategies/{new['id']}"
    assert new["markets"] == ["ETH-EUR"] and new["budget_eur"] == 150
    assert new["available_eur"] == 150
    # The first strategy now trades the rest.
    (first, second) = (await api.get("/api/strategies")).json()
    assert first["active_markets"] == ["BTC-EUR"] and second["active_markets"] == ["ETH-EUR"]
    assert (await api.get(f"/api/strategies/{new['id']}")).json()["name"] == "ETH-dip"

    # Refused: a market of another strategy, an unknown market, a second catch-all.
    r = await api.post("/api/strategies", json=body)
    assert r.status_code == 422 and r.json()["code"] == "strategy_conflict"
    r = await api.post("/api/strategies", json=body | {"markets": ["XRP-EUR"]})
    assert r.status_code == 422 and r.json()["code"] == "market_not_tracked"
    r = await api.post("/api/strategies", json=body | {"markets": []})
    assert r.status_code == 422 and "alle overige markten" in r.json()["detail"]
    r = await api.post("/api/strategies", json=body | {"markets": ["SOL-EUR"], "budget_eur": 2})
    assert r.status_code == 422 and r.json()["code"] == "invalid_input"
    no_stop = body["strategy"] | {"stop_loss_pct": None, "take_profit_pct": None}
    r = await api.post("/api/strategies", json={"strategy": no_stop, "markets": ["SOL-EUR"]})
    assert r.status_code == 422 and r.json()["code"] == "invalid_strategy"

    # A partial update: only the budget, then the switch, then the rules.
    r = await api.put(f"/api/strategies/{new['id']}", json={"budget_eur": 60})
    assert r.status_code == 200 and r.json()["budget_eur"] == 60
    assert r.json()["markets"] == ["ETH-EUR"]
    r = await api.put(f"/api/strategies/{new['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["active_markets"] == []
    (first, _) = (await api.get("/api/strategies")).json()
    assert first["active_markets"] == MARKETS  # the rest again
    r = await api.put(
        f"/api/strategies/{new['id']}", json={"strategy": body["strategy"] | {"name": "ETH v2"}}
    )
    assert r.json()["name"] == "ETH v2" and r.json()["previous_name"] == "ETH-dip"
    r = await api.put(
        f"/api/strategies/{new['id']}", json={"markets": ["BTC-EUR"], "enabled": True}
    )
    assert r.status_code == 200 and r.json()["active_markets"] == ["BTC-EUR"]
    r = await api.put(f"/api/strategies/{first['id']}", json={"markets": ["BTC-EUR"]})
    assert r.status_code == 422 and r.json()["code"] == "strategy_conflict"
    assert (await api.get("/api/strategies/999")).status_code == 404
    assert (await api.put("/api/strategies/999", json={"budget_eur": 1})).status_code == 404

    # The old one-strategy endpoints work on the first strategy.
    assert (await api.get("/api/strategy")).json()["name"] == DEFAULT_STRATEGY.name
    r = await api.put("/api/strategy", json=cross_strategy(name="Eerste").model_dump(mode="json"))
    assert r.status_code == 200 and (await api.get("/api/strategies")).json()[0]["name"] == "Eerste"

    r = await api.delete(f"/api/strategies/{new['id']}")
    assert r.status_code == 200 and r.json() == {"deleted": True, "id": new["id"]}
    assert (await api.delete(f"/api/strategies/{first['id']}")).status_code == 409
    assert len((await api.get("/api/strategies")).json()) == 1


async def test_the_strategy_page_lists_adds_configures_and_deletes_strategies(api):
    page = (await api.get("/strategy")).text
    assert 'id="strategieen"' in page and "1 van 10" in page
    assert "alle overige markten" in page and "geen eigen budget" in page

    # Add: with the default rules, on ETH-EUR, budget 200; the page opens the new one.
    r = await api.post(
        "/strategy",
        data={"action": "add", "markets": "eth-eur", "budget_eur": "200"},
        headers=HTML,
    )
    assert r.status_code == 303
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "ok" and "ETH-EUR" in msg["text"] and "200" in msg["text"]
    location = r.headers["location"]
    assert location.startswith("/strategy?id=") and location.endswith("#actief")
    new_id = int(location.split("id=")[1].split("#")[0])
    page = (await api.get(location.split("#")[0])).text
    assert "2 van 10" in page and "Nieuwe strategie" in page
    assert f'name="strategy_id" value="{new_id}"' in page
    assert "Geldt voor <strong>ETH-EUR</strong>" in page
    # The first strategy now trades the rest; the overview says so.
    assert 'BTC-EUR <span class="muted">(alle overige markten)</span>' in page

    # Refused: the same market twice; the wording explains.
    r = await api.post("/strategy", data={"action": "add", "markets": "ETH-EUR"}, headers=HTML)
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error" and "hoort al bij strategie" in msg["text"]
    await api.get("/strategy")  # shows (and so clears) that message

    # Change budget and markets of the new one; switch it off.
    r = await api.post(
        "/strategy",
        data={
            "action": "slot_settings",
            "strategy_id": new_id,
            "markets": "ETH-EUR",
            "budget_eur": "75",
        },
        headers=HTML,
    )
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "ok" and "budget € 75" in msg["text"] and "uit." in msg["text"]
    with SessionLocal() as s:
        slot = next(x for x in load_slots(s) if x.id == new_id)
        assert slot.budget_eur == 75 and not slot.enabled and slot.markets == ["ETH-EUR"]
    page = (await api.get("/strategy")).text
    assert "Uit</span>" in page
    r = await api.post("/strategy", data={"action": "enable", "strategy_id": new_id}, headers=HTML)
    assert "aangezet" in flashes_from_response(r)[0]["text"]
    await api.get("/strategy")

    # Saving rules for the second strategy leaves the first alone.
    rules = cross_strategy(name="ETH-regels").model_dump_json()
    r = await api.post(
        "/strategy",
        data={"action": "save", "strategy_id": new_id, "strategy_json": rules},
        headers=HTML,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/strategy?id={new_id}#actief"
    assert "ETH-regels" in flashes_from_response(r)[0]["text"]
    await api.get("/strategy")
    with SessionLocal() as s:
        names = [x.name for x in load_slots(s)]
        assert names == [DEFAULT_STRATEGY.name, "ETH-regels"]

    # Delete: the page goes back to the list; the last one cannot go.
    r = await api.post("/strategy", data={"action": "delete", "strategy_id": new_id}, headers=HTML)
    assert r.headers["location"] == "/strategy#strategieen"
    assert "verwijderd" in flashes_from_response(r)[0]["text"]
    await api.get("/strategy")
    with SessionLocal() as s:
        (only,) = load_slots(s)
    r = await api.post("/strategy", data={"action": "delete", "strategy_id": only.id}, headers=HTML)
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error" and "laatste strategie" in msg["text"]


async def test_the_pages_say_when_a_market_has_no_strategy(api):
    with SessionLocal() as s:
        first = load_slots(s)[0]
        save_slot(s, first.id, markets=["BTC-EUR"], budget_eur=100)
    for path in ("/strategy", "/trading", "/plan"):
        page = (await api.get(path)).text
        assert "Geen strategie" in page or "geen strategie handelt daar" in page, path
    page = (await api.get("/")).text
    assert "budget €" in page and "100" in page[page.index("budget €") :][:12]


async def test_mcp_tools_cover_the_strategies(api):
    from app.mcp_server import TOOLS_BY_NAME

    for name in ("strategies", "add_strategy", "update_strategy", "remove_strategy"):
        assert name in TOOLS_BY_NAME
    tool = TOOLS_BY_NAME["set_strategy"]
    assert "strategy_id" in tool.schema()["properties"]
    strategy = Strategy.model_validate(DEFAULT_STRATEGY.model_dump())
    args = tool.model(strategy=strategy, strategy_id=None)
    assert tool.path(args) == "/api/strategy" and tool.body(args)["name"] == strategy.name
    args = tool.model(strategy=strategy, strategy_id=3)
    assert tool.path(args) == "/api/strategies/3" and tool.body(args)["strategy"]["name"]
