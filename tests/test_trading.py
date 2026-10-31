from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetExceeded, BudgetGuard
from app.ai.decision_maker import DecisionMaker, TradeDecision
from app.ai.evaluator import EvaluationResult, Evaluator
from app.bitvavo.broker import PaperBroker
from app.db import Base, Decision, NewsItem, Position, make_engine
from app.strategy.model import Strategy
from app.strategy.store import save_strategy
from app.trading import risk
from app.trading.engine import (
    TradingEngine,
    latest_decisions,
    open_position,
    performance_summary,
    recent_decisions,
)
from app.trading.risk import RiskLimits

LIMITS = RiskLimits(
    max_trade_eur=100,
    max_exposure_pct=50,
    daily_loss_limit_pct=5,
    max_trades_per_day=3,
    cooldown_min_after_stop=240,
    min_trade_eur=10,
)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def candles(closes, step=3_600_000):
    t0 = 1_700_000_000_000
    return [[t0 + i * step, c, c * 1.01, c * 0.99, float(c), 1.0] for i, c in enumerate(closes)]


class FakeClient:
    """Serves one fixed candle series per market and a current price."""

    def __init__(self, series_by_market, prices):
        self.series = series_by_market
        self.prices = prices

    async def ticker_prices(self, markets):
        return {m: self.prices[m] for m in markets if m in self.prices}

    async def ticker_price(self, market):
        return self.prices[market]

    async def candles(self, market, interval, limit):
        return self.series[market]


class FakeDecider(DecisionMaker):
    def __init__(self, decision=None, error=None, available=True):
        super().__init__("key" if available else "", "claude-sonnet-5-5", BudgetGuard(100))
        self.decision = decision
        self.error = error
        self.contexts = []

    async def decide(self, session, context):
        if self._client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.decision


class FakeEvaluator(Evaluator):
    def __init__(self, result):
        super().__init__("key", "claude-fable-5-1", BudgetGuard(100))
        self.result = result
        self.contexts = []

    async def evaluate(self, session, context):
        self.contexts.append(context)
        return self.result


def cross_strategy(**overrides) -> Strategy:
    base = {
        "name": "SMA cross",
        "market": "BTC-EUR",
        "interval": "1h",
        "indicators": [
            {"type": "sma", "id": "fast", "period": 2},
            {"type": "sma", "id": "slow", "period": 4},
        ],
        "buy": [{"left": "fast", "op": "crosses_above", "right": "slow"}],
        "sell": [{"left": "fast", "op": "crosses_below", "right": "slow"}],
        "stop_loss_pct": 10,
        "take_profit_pct": 30,
    }
    base.update(overrides)
    return Strategy.model_validate(base)


BUY_SERIES = [10, 9, 8, 7, 6, 9, 12]  # fast crosses above slow on the last bar
SELL_SERIES = [6, 9, 12, 15, 18, 15, 12]  # fast crosses below slow on the last bar
FLAT_SERIES = [10, 10, 10, 10, 10, 10, 10]


def make_engine_(
    session, series, prices, decider, strategy=None, evaluator=None, mode="hold", fee_pct=0.0
):
    save_strategy(session, strategy or cross_strategy())
    client = FakeClient(series, prices)
    broker = PaperBroker(client, start_eur=1000, fee_pct=fee_pct)
    return TradingEngine(
        client,
        broker,
        decider,
        evaluator or FakeEvaluator(EvaluationResult(report_nl="x")),
        list(series),
        LIMITS,
        claude_unavailable_mode=mode,
        fee_pct=fee_pct,
    )


# -- risk ------------------------------------------------------------------------------


def test_check_buy_caps_amount_and_exposure(session):
    allowed, why = risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 250, 1000, 0)
    assert (allowed, why) == (100.0, None)
    # exposure room: 50% of 1000 = 500, already 450 in coins -> 50 left
    allowed, why = risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 100, 1000, 450)
    assert (allowed, why) == (50.0, None)
    allowed, why = risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 100, 1000, 495)
    assert allowed == 0 and "blootstelling" in why


def test_check_buy_pause_daily_limits_and_cooldown(session):
    risk.set_paused(session, True)
    assert "gepauzeerd" in risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 50, 1000, 0)[1]
    risk.set_paused(session, False)

    now = datetime.now(UTC)
    for _ in range(3):
        session.add(
            Decision(
                mode="paper",
                market="BTC-EUR",
                price=1,
                signal="buy",
                source="claude",
                action="buy",
                executed=True,
                created_at=now,
            )
        )
    session.commit()
    assert "trades per dag" in risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 50, 1000, 0)[1]
    assert risk.check_buy(session, LIMITS, "live", "BTC-EUR", 50, 1000, 0)[1] is None

    session.add(
        Position(
            mode="live",
            market="ETH-EUR",
            status="closed",
            amount=1,
            entry_price=100,
            entry_quote=100,
            exit_price=80,
            exit_quote=80,
            pnl_eur=-60,
            pnl_pct=-60,
            closed_at=now,
            close_reason="stop_loss",
        )
    )
    session.commit()
    assert "dagverlies" in risk.check_buy(session, LIMITS, "live", "BTC-EUR", 50, 1000, 0)[1]
    # bigger portfolio: loss is only 0.6%, but ETH is in cooldown after its stop-loss
    assert risk.check_buy(session, LIMITS, "live", "BTC-EUR", 50, 10_000, 0)[1] is None
    assert "afkoel" in risk.check_buy(session, LIMITS, "live", "ETH-EUR", 50, 10_000, 0)[1]


# -- engine ------------------------------------------------------------------------------


async def test_hold_without_signal_does_not_call_claude(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=50, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and result.source == "rules" and not result.executed
    assert decider.contexts == []
    (d,) = session.query(Decision).all()
    assert d.signal == "hold" and d.news_label == "geen nieuws"


async def test_buy_signal_claude_buys_and_opens_position(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=500, reasoning_nl="Sterk signaal.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    (result,) = await engine.run_cycle(session)
    assert result.signal == "buy" and result.action == "buy" and result.executed
    ctx = decider.contexts[0]
    assert ctx["signal"] == "buy" and ctx["position"] is None and "recent_performance" in ctx
    pos = open_position(session, "paper", "BTC-EUR")
    assert pos is not None
    assert pos.entry_quote == 100.0  # capped by MAX_TRADE_EUR, not Claude's 500
    assert pos.stop_price == pytest.approx(12.0 * 0.9) and pos.take_profit_price == pytest.approx(
        15.6
    )
    (d,) = session.query(Decision).all()
    assert d.source == "claude" and d.executed and d.amount_quote == 100.0 and d.order_id
    assert (await engine.broker.balances(session))["EUR"] == pytest.approx(900)


async def test_claude_cannot_buy_without_signal(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=50, reasoning_nl="x")
    )
    # SELL series with an open position -> Claude is consulted, but a buy is illegal here.
    engine = make_engine_(session, {"BTC-EUR": candles(SELL_SERIES)}, {"BTC-EUR": 9.0}, decider)
    session.add(
        Position(
            mode="paper", market="BTC-EUR", amount=1, entry_price=8, entry_quote=8, stop_price=1
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and not result.executed
    assert "niet toegestaan" in result.reason
    assert open_position(session, "paper", "BTC-EUR") is not None


async def test_sell_signal_closes_position_with_pnl(session):
    decider = FakeDecider(
        TradeDecision(action="sell", confidence=0.9, amount_quote=0, reasoning_nl="Exit.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(SELL_SERIES)}, {"BTC-EUR": 9.0}, decider)
    # Give the paper wallet the coins it is about to sell.
    await engine.broker.market_order(session, "BTC-EUR", "buy", 90, reason="setup")
    session.add(
        Position(
            mode="paper", market="BTC-EUR", amount=10, entry_price=8, entry_quote=80, stop_price=1
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "sell" and result.executed
    pos = session.query(Position).one()
    assert pos.status == "closed" and pos.close_reason == "signal"
    assert pos.exit_quote == pytest.approx(90) and pos.pnl_eur == pytest.approx(10)
    assert pos.pnl_pct == pytest.approx(12.5)


async def test_fees_flow_into_position_pnl_and_claude_context(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=100, reasoning_nl="Go.")
    )
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, fee_pct=0.25
    )
    await engine.run_cycle(session)
    ctx = decider.contexts[0]
    assert ctx["costs"]["fee_pct_per_side"] == 0.25 and ctx["costs"]["round_trip_pct"] == 0.5
    pos = open_position(session, "paper", "BTC-EUR")
    assert pos.entry_quote == pytest.approx(100) and pos.fees_eur == pytest.approx(0.25)
    assert pos.amount == pytest.approx(99.75 / 12)

    # Sell at the same price via the engine: the round trip loses exactly the two fees.
    decider.decision = TradeDecision(action="sell", confidence=1, amount_quote=0, reasoning_nl="x")
    engine.client.series["BTC-EUR"] = candles(SELL_SERIES)
    await engine.run_cycle(session)
    ctx = decider.contexts[1]
    assert ctx["position"]["unrealized_net_pct"] == pytest.approx(-0.499, abs=0.01)
    pos = session.query(Position).one()
    assert pos.status == "closed"
    assert pos.fees_eur == pytest.approx(0.25 + 99.75 * 0.0025)
    assert pos.pnl_eur == pytest.approx(-pos.fees_eur)
    assert pos.exit_quote == pytest.approx(99.75 * 0.9975)
    perf = performance_summary(session, "paper")
    assert perf["total_fees_eur"] == pytest.approx(round(pos.fees_eur, 2))


def test_add_missing_columns_adds_new_fee_columns():
    from sqlalchemy import inspect, text

    from app.db import add_missing_columns

    engine = make_engine("sqlite:///:memory:")
    with engine.begin() as conn:  # an "old" orders table without fee_quote
        conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, mode VARCHAR(8))"))
    add_missing_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("orders")}
    assert "fee_quote" in cols
    add_missing_columns(engine)  # idempotent


async def test_stop_loss_is_mechanical_and_skips_claude(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    await engine.broker.market_order(session, "BTC-EUR", "buy", 50, reason="setup")
    session.add(
        Position(
            mode="paper",
            market="BTC-EUR",
            amount=5,
            entry_price=12,
            entry_quote=60,
            stop_price=10.5,
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert (
        result.source == "mechanical" and result.action == "sell" and result.reason == "stop_loss"
    )
    assert decider.contexts == []
    pos = session.query(Position).one()
    assert pos.status == "closed" and pos.pnl_eur == pytest.approx(-10)


async def test_news_exit_when_high_impact_negative_news(session):
    decider = FakeDecider(
        TradeDecision(action="sell", confidence=0.7, amount_quote=0, reasoning_nl="Hack.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    await engine.broker.market_order(session, "BTC-EUR", "buy", 50, reason="setup")
    session.add(Position(mode="paper", market="BTC-EUR", amount=5, entry_price=10, entry_quote=50))
    session.add(
        NewsItem(
            source="rss",
            source_name="x",
            url="u",
            title="Exchange hacked",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            assessed=True,
            relevant=True,
            sentiment=-0.9,
            impact=3,
            horizon="days",
            coins="ALL",
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.executed and result.action == "sell"
    assert decider.contexts[0]["news"]["label"] == "negatief"
    assert session.query(Position).one().close_reason == "news_exit"


def positive_news(sentiment=0.9):
    return NewsItem(
        source="rss",
        source_name="x",
        url="u2",
        title="Spot ETF approved",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        assessed=True,
        relevant=True,
        sentiment=sentiment,
        impact=3,
        horizon="days",
        coins="BTC",
    )


async def test_strong_positive_news_lets_claude_buy_a_smaller_position(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=100, reasoning_nl="ETF.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    session.add(positive_news())
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.signal == "hold" and result.executed and result.action == "buy"
    assert decider.contexts[0]["news_entry_allowed"] is True
    # News alone never gets the full trade budget: half of max_trade_eur.
    assert open_position(session, "paper", "BTC-EUR").entry_quote == 50.0


async def test_mild_positive_news_without_signal_skips_claude(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=100, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    session.add(positive_news(sentiment=0.3))
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and result.source == "rules"
    assert decider.contexts == []


async def test_claude_buy_without_signal_or_news_is_blocked(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=100, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(SELL_SERIES)}, {"BTC-EUR": 12.0}, decider)
    session.add(Position(mode="paper", market="BTC-EUR", amount=5, entry_price=10, entry_quote=50))
    await engine.broker.market_order(session, "BTC-EUR", "buy", 50, reason="setup")
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and not result.executed
    assert "open positie" in result.reason


async def test_rules_hold_explains_every_buy_condition(session):
    decider = FakeDecider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and decider.contexts == []
    d = session.query(Decision).one()
    assert d.reasoning.startswith("Geen koopsignaal, 1 van 1 harde koopvoorwaarden niet gehaald")
    assert "Claude" in d.reasoning
    assert "✗" in d.reasoning and "geen beoordeeld nieuws" in d.reasoning.lower()
    checks = d.check_list()
    assert [c["ok"] for c in checks["buy"]] == [False]
    assert checks["buy"][0]["text"] == "fast kruist omhoog door slow"
    assert checks["buy"][0]["left"] == 10.0 and checks["buy"][0]["right"] == 10.0
    assert latest_decisions(session, "paper", ["ETH-EUR", "BTC-EUR"]) == [d]
    assert recent_decisions(session, "paper", days=1) == [d]
    assert recent_decisions(session, "paper", days=0) == []


async def test_failed_soft_filter_still_asks_claude_and_halves_the_buy(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.6, amount_quote=100, reasoning_nl="Dip.")
    )
    strategy = cross_strategy(
        buy=[
            {"left": "fast", "op": "crosses_above", "right": "slow"},
            {"left": "close", "op": ">", "right": 1000, "soft": True},  # never holds here
        ]
    )
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, strategy=strategy
    )
    (result,) = await engine.run_cycle(session)
    assert result.signal == "buy" and result.executed and result.action == "buy"
    ctx = decider.contexts[0]
    assert ctx["soft_filters_failed"] == ["koers hoger dan 1.000 (zacht filter)"]
    traj = ctx["recent_candles"]
    assert traj["values"]["close"] == BUY_SERIES[-6:] and traj["close_falling"] is False
    assert traj["close_change_24h_pct"] == 20.0  # 10 -> 12 over the whole short series
    pc = traj["price_context"]  # FakeClient serves the same candles for "1d"
    assert pc["below_30d_high_pct"] == 0.99 and pc["change_30d_pct"] == 20.0  # high = close*1.01
    assert ctx["signal_checks"]["buy"][1]["soft"] is True
    assert open_position(session, "paper", "BTC-EUR").entry_quote == 50.0
    d = session.query(Decision).one()
    assert d.reasoning == "Dip." and d.source == "claude"
    assert d.check_list()["buy"][1]["ok"] is False


def dip_strategy():
    return cross_strategy(
        indicators=[{"type": "sma", "id": "fast", "period": 2}],
        buy=[
            {"left": "close", "op": "<", "right": 8},  # the dip
            {"left": "close", "op": "rising", "right": 1},  # the turn
        ],
        sell=[{"left": "close", "op": ">", "right": 100}],
    )


async def test_dip_watch_arms_on_the_dip_and_hands_the_turn_to_claude(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.7, amount_quote=100, reasoning_nl="Draai.")
    )
    falling = [10, 9, 8, 7, 6]
    engine = make_engine_(
        session, {"BTC-EUR": candles(falling)}, {"BTC-EUR": 6.0}, decider, strategy=dip_strategy()
    )
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and decider.contexts == []
    assert "BTC-EUR" in engine.watch and "koers lager dan 8" in engine.watch["BTC-EUR"]["dip"]
    assert engine.watch["BTC-EUR"]["waiting_for"] == "koers stijgt t.o.v. 1 candle eerder"
    assert "dipbewaking actief" in session.query(Decision).one().reasoning

    # Still falling: a quiet watch check, no journal entry, still armed.
    engine.client.series["BTC-EUR"] = candles(falling + [5.5])
    (check,) = await engine.run_watch(session)
    assert check.source == "watch" and not check.executed
    assert session.query(Decision).count() == 1 and "BTC-EUR" in engine.watch

    # The turn: price ticks up -> Claude is asked right away and buys.
    engine.client.series["BTC-EUR"] = candles(falling + [5.5, 6.2])
    engine.client.prices["BTC-EUR"] = 6.2
    (fired,) = await engine.run_watch(session)
    assert fired.signal == "buy" and fired.executed and fired.source == "claude"
    assert decider.contexts[0]["signal"] == "buy"
    assert "BTC-EUR" not in engine.watch and "BTC-EUR" in engine.watch_cooldown
    d = session.query(Decision).order_by(Decision.id.desc()).first()
    assert d.signal == "buy" and d.executed and d.source == "claude"
    assert open_position(session, "paper", "BTC-EUR") is not None


async def test_dip_watch_gives_up_after_watch_max_min(session):
    decider = FakeDecider()
    engine = make_engine_(
        session,
        {"BTC-EUR": candles([10, 9, 8, 7, 6])},
        {"BTC-EUR": 6.0},
        decider,
        strategy=dip_strategy(),
    )
    engine.watch_max_min = 30
    await engine.run_cycle(session)
    engine.watch["BTC-EUR"]["since"] = datetime.now(UTC) - timedelta(minutes=31)
    assert await engine.run_watch(session)
    assert "BTC-EUR" not in engine.watch and decider.contexts == []


async def test_strategy_without_turn_condition_never_arms(session):
    decider = FakeDecider()
    engine = make_engine_(
        session, {"BTC-EUR": candles([10, 9, 8, 7, 6])}, {"BTC-EUR": 6.0}, decider
    )
    await engine.run_cycle(session)
    assert engine.watch == {} and await engine.run_watch(session) == []


async def test_fallback_hold_when_claude_unavailable(session):
    decider = FakeDecider(available=False)
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and result.source == "rules" and not result.executed
    assert "ANTHROPIC_API_KEY" in engine.last_error


async def test_fallback_indicators_follows_signal_when_budget_exhausted(session):
    decider = FakeDecider(error=BudgetExceeded("budget op"))
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, mode="indicators"
    )
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and result.executed and result.source == "rules"
    assert open_position(session, "paper", "BTC-EUR").entry_quote == 100.0


async def test_paused_blocks_buys_but_allows_signal_sell(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=50, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    risk.set_paused(session, True)
    (result,) = await engine.run_cycle(session)
    assert not result.executed and "gepauzeerd" in result.reason


async def test_multi_market_cycle_isolates_errors(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    engine = make_engine_(
        session,
        {"BTC-EUR": candles(FLAT_SERIES), "ETH-EUR": candles(FLAT_SERIES)},
        {"BTC-EUR": 10.0},  # no ETH price -> candles close is used; make candles fail instead
        decider,
    )

    async def broken(market, interval, limit):
        if market == "ETH-EUR":
            raise RuntimeError("boom")
        return candles(FLAT_SERIES)

    engine.client.candles = broken
    results = await engine.run_cycle(session)
    assert [r.market for r in results] == ["BTC-EUR", "ETH-EUR"]
    assert results[1].source == "error" and "boom" in results[1].reason
    assert engine.last_run is not None and len(engine.last_results) == 2


async def test_update_outcomes_and_performance_summary(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 11.0}, decider)
    now = datetime.now(UTC)
    old = Decision(
        mode="paper",
        market="BTC-EUR",
        price=10.0,
        signal="buy",
        source="claude",
        action="buy",
        executed=True,
        created_at=now - timedelta(hours=25),
    )
    fresh = Decision(
        mode="paper",
        market="BTC-EUR",
        price=10.0,
        signal="buy",
        source="claude",
        action="hold",
        created_at=now - timedelta(minutes=10),
    )
    session.add_all([old, fresh])
    session.add(
        Position(
            mode="paper",
            market="BTC-EUR",
            status="closed",
            amount=1,
            entry_price=10,
            entry_quote=10,
            exit_price=12,
            exit_quote=12,
            pnl_eur=2,
            pnl_pct=20,
            closed_at=now,
            close_reason="signal",
        )
    )
    session.commit()
    assert await engine.update_outcomes(session, {"BTC-EUR": 11.0}) == 1
    assert old.price_1h == 11.0 and old.price_24h == 11.0 and old.price_72h is None
    assert fresh.price_1h is None
    perf = performance_summary(session, "paper")
    assert perf["closed_positions"] == 1 and perf["win_rate_pct"] == 100.0
    executed = next(d for d in perf["recent_decisions"] if d["executed"])
    assert executed["move_24h_pct"] == 10.0 and executed["move_1h_pct"] == 10.0


async def test_run_evaluation_stores_report_and_backtests_proposal(session):
    proposal = cross_strategy(name="Tighter stop", stop_loss_pct=5)
    evaluator = FakeEvaluator(
        EvaluationResult(
            report_nl="Goed gegaan.",
            proposed_strategy=proposal,
            proposal_rationale_nl="Minder verlies.",
        )
    )
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    series = candles([10, 9, 8, 7, 6, 9, 12, 15, 18, 15, 12, 9, 6, 5])
    engine = make_engine_(
        session, {"BTC-EUR": series}, {"BTC-EUR": 5.0}, decider, evaluator=evaluator
    )
    ev = await engine.run_evaluation(session, days=7)
    assert "Goed gegaan" in ev.report_nl and "Minder verlies" in ev.report_nl
    assert ev.model == "claude-fable-5-1" and not ev.applied
    assert Strategy.model_validate_json(ev.proposed_strategy).stop_loss_pct == 5
    import json

    bt = json.loads(ev.proposal_backtest)
    assert "return_pct" in bt["proposed"] and "return_pct" in bt["current"]
    assert evaluator.contexts[0]["strategy"]["name"] == "SMA cross"
