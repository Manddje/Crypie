from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetExceeded, BudgetGuard
from app.ai.decision_maker import DecisionMaker, TradeDecision
from app.ai.evaluator import EvaluationResult, Evaluator
from app.bitvavo.broker import PaperBroker
from app.db import Base, Decision, NewsItem, Order, Position, make_engine
from app.news.service import save_etf_flows
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
from app.trading.events import MarketEvent
from app.trading.risk import RiskLimits
from app.trading.trend import save_trend

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
    # Every filled order of today counts (bot, exit loop or manual), plus unsettled ones.
    for status, amount in (("filled", 1.0), ("rejected", None), ("filled", 0.0), ("unknown", None)):
        session.add(
            Order(
                mode="paper",
                market="BTC-EUR",
                side="buy",
                order_type="market",
                status=status,
                amount=amount,
                created_at=now,
            )
        )
    session.commit()
    assert risk.trades_today(session, "paper") == 2
    assert risk.check_buy(session, LIMITS, "paper", "BTC-EUR", 50, 1000, 0)[1] is None
    session.add(
        Order(
            mode="paper",
            market="ETH-EUR",
            side="sell",
            order_type="market",
            status="filled",
            amount=1.0,
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
    # News alone only moves money when two different outlets report it (F36).
    for outlet in ("CoinDesk", "The Block"):
        session.add(
            NewsItem(
                source="rss",
                source_name=outlet,
                url=f"https://{outlet}/hack",
                title="Exchange hacked",
                published_at=datetime.now(UTC) - timedelta(hours=1),
                assessed=True,
                relevant=True,
                sentiment=-0.9,
                impact=3,
                horizon="days",
                coins="ALL",
                event_key="exchange-hack",
            )
        )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.executed and result.action == "sell"
    assert decider.contexts[0]["news"]["label"] == "negatief"
    assert session.query(Position).one().close_reason == "news_exit"


def positive_news(sentiment=0.9, outlet="x"):
    return NewsItem(
        source="rss",
        source_name=outlet,
        url=f"https://{outlet}/etf",
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
    session.add_all([positive_news(outlet="CoinDesk"), positive_news(outlet="Decrypt")])
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
    assert engine.last_error == "er is geen Claude-sleutel"
    assert "Claude niet beschikbaar: er is geen Claude-sleutel; niets gedaan" in result.reason


async def test_fallback_indicators_follows_signal_when_budget_exhausted(session):
    decider = FakeDecider(error=BudgetExceeded("budget op"))
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, mode="indicators"
    )
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and result.executed and result.source == "rules"
    # Without Claude the signal is followed with half the budget (F43).
    assert open_position(session, "paper", "BTC-EUR").entry_quote == 50.0


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
    today = datetime.now(UTC).date()
    save_etf_flows(
        session,
        {
            "coin": "BTC",
            "as_of": today.isoformat(),
            "sum_5d_musd": -300.0,
            "sum_30d_musd": 900.0,
            "typical_day_musd": 40.0,
            "days": 12,
            "last_day_musd": -100.0,
            "daily": [
                {"date": (today - timedelta(days=i)).isoformat(), "flow_musd": float(-i)}
                for i in range(11, -1, -1)
            ],
        },
    )
    ev = await engine.run_evaluation(session, days=7)
    # The ETF flows of the period: the summary plus the daily series cut to the last 7 days.
    flows = evaluator.contexts[0]["etf_flows"]["BTC"]
    assert flows["label"] == "strong_outflow" and flows["sum_30d_musd"] == 900.0
    assert len(flows["daily_in_period"]) == 8  # today and the 7 days before
    assert flows["daily_in_period"][-1]["flow_musd"] == 0.0
    assert "daily" not in flows and "note" in flows
    assert "Goed gegaan" in ev.report_nl and "Minder verlies" in ev.report_nl
    assert ev.model == "claude-fable-5-1" and not ev.applied
    assert Strategy.model_validate_json(ev.proposed_strategy).stop_loss_pct == 5
    import json

    bt = json.loads(ev.proposal_backtest)
    assert "return_pct" in bt["proposed"] and "return_pct" in bt["current"]
    assert evaluator.contexts[0]["strategy"]["name"] == "SMA cross"

    engine.etf_flows_enabled = False
    await engine.run_evaluation(session, days=7)
    assert evaluator.contexts[1]["etf_flows"] is None


async def test_long_term_trend_reaches_claude_when_enabled(session):
    save_trend(session, {"market": "BTC-EUR", "regime": "down", "regime_nl": "neerwaarts"})
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=0.5, amount_quote=0, reasoning_nl="Wacht.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    await engine.run_cycle(session)
    assert decider.contexts[0]["long_term_trend"]["regime"] == "down"

    engine.trend_context = False
    await engine.run_cycle(session)
    assert decider.contexts[1]["long_term_trend"] is None


async def test_etf_flows_reach_claude_when_enabled(session):
    save_etf_flows(
        session,
        {
            "coin": "BTC",
            "as_of": datetime.now(UTC).date().isoformat(),
            "sum_5d_musd": -300.0,
            "typical_day_musd": 40.0,
            "days": 5,
            "last_day_musd": -100.0,
        },
    )
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=0.5, amount_quote=0, reasoning_nl="Wacht.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    await engine.run_cycle(session)
    flows = decider.contexts[0]["etf_flows"]
    assert flows["label"] == "strong_outflow" and flows["sum_5d_musd"] == -300.0

    engine.etf_flows_enabled = False
    await engine.run_cycle(session)
    assert decider.contexts[1]["etf_flows"] is None


async def test_risk_off_window_blocks_buys_without_asking_claude(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.9, amount_quote=100, reasoning_nl="Kopen.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    engine.calendar = [MarketEvent(datetime.now(UTC) + timedelta(minutes=30), "FOMC-rentebesluit")]
    (result,) = await engine.run_cycle(session)
    assert result.signal == "buy" and result.action == "hold" and result.source == "rules"
    assert "Risk-off: FOMC-rentebesluit" in result.reason and decider.contexts == []
    (d,) = session.query(Decision).all()
    assert d.blocked_by == "risk-off" and open_position(session, "paper", "BTC-EUR") is None

    # Switched off: the same cycle buys.
    engine.risk_off_enabled = False
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and result.executed


def _shock_news(session, key, hours_ago_by_outlet):
    for hours_ago, outlet in hours_ago_by_outlet:
        session.add(
            NewsItem(
                source="rss",
                source_name=outlet,
                url=f"https://{outlet}/{key}",
                title="Exchange hacked",
                summary_nl="Grote beurs gehackt.",
                published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                assessed=True,
                relevant=True,
                sentiment=-0.9,
                impact=3,
                coins="ALL",
                event_key=key,
            )
        )
    session.commit()


async def test_news_shock_pauses_buying_but_not_selling(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.9, amount_quote=100, reasoning_nl="Kopen.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    # A shock needs two outlets reporting the same event (F37).
    _shock_news(session, "exchange-hack", ((1, "coindesk.com"), (2, "theblock.co")))
    (result,) = await engine.run_cycle(session)
    # No Claude for the shock check in this engine: the fixed pause (SHOCK_PAUSE_HOURS) applies.
    assert result.action == "hold"
    assert "Risk-off: nieuwsschok exchange-hack sinds" in result.reason
    assert "vaste pauze, geen nieuwe aankopen tot" in result.reason
    assert decider.contexts == []

    # The fixed duration follows the setting: at 0 hours the pause is over and the bot buys.
    engine.shock_pause_hours = 0
    (result,) = await engine.run_cycle(session)
    assert result.action == "buy" and result.executed

    # With an open position a (new) shock does not block Claude: selling must stay possible.
    decider.decision = TradeDecision(
        action="sell", confidence=0.9, amount_quote=0, reasoning_nl="Eruit."
    )
    engine2 = make_engine_(session, {"BTC-EUR": candles(SELL_SERIES)}, {"BTC-EUR": 12.0}, decider)
    engine2.shock_pause_hours = 6
    _shock_news(session, "exchange-hack-2", ((0.2, "coindesk.com"), (0.1, "theblock.co")))
    (result,) = await engine2.run_cycle(session)
    assert result.action == "sell" and result.executed
    # Claude saw the shock in its context while deciding about the open position.
    assert decider.contexts[-1]["market_events"]["risk_off"]["kind"] == "shock"


async def test_trailing_stop_moves_up_with_the_price_and_closes_with_its_own_reason(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    strategy = cross_strategy(stop_loss_pct=10, trailing_stop_pct=5, trailing_start_pct=2)
    prices = {"BTC-EUR": 12.0}
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, prices, decider, strategy)
    await engine.broker.market_order(session, "BTC-EUR", "buy", 60, reason="setup")  # 5 BTC
    session.add(
        Position(
            mode="paper",
            market="BTC-EUR",
            amount=5,
            entry_price=10,
            entry_quote=50,
            stop_price=9.0,
            high_price=10,
        )
    )
    session.commit()

    # +20%: trailing is armed; the stop follows 5% under the new high and the position stays.
    (result,) = await engine.run_cycle(session)
    pos = session.query(Position).one()
    assert result.action == "hold" and pos.status == "open"
    assert pos.high_price == 12.0 and pos.stop_price == pytest.approx(11.4)

    # A dip that stays above the trailed stop: the stop never moves down.
    prices["BTC-EUR"] = 11.5
    await engine.run_cycle(session)
    pos = session.query(Position).one()
    assert pos.high_price == 12.0 and pos.stop_price == pytest.approx(11.4) and pos.status == "open"

    # Through the trailed stop: mechanical exit, at a profit, labelled trailing_stop.
    prices["BTC-EUR"] = 11.3
    (result,) = await engine.run_cycle(session)
    pos = session.query(Position).one()
    assert result.source == "mechanical" and result.reason == "trailing_stop"
    assert pos.status == "closed" and pos.close_reason == "trailing_stop" and pos.pnl_eur > 0
    assert decider.contexts == []


async def test_trailing_stop_not_armed_below_start_threshold(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    strategy = cross_strategy(stop_loss_pct=10, trailing_stop_pct=3, trailing_start_pct=5)
    engine = make_engine_(
        session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.3}, decider, strategy
    )
    await engine.broker.market_order(session, "BTC-EUR", "buy", 50, reason="setup")
    session.add(
        Position(
            mode="paper", market="BTC-EUR", amount=5, entry_price=10, entry_quote=50, stop_price=9
        )
    )
    session.commit()
    await engine.run_cycle(session)
    pos = session.query(Position).one()
    assert pos.high_price == 10.3 and pos.stop_price == 9  # +3% is under the +5% start


async def test_new_position_records_its_high_and_initial_stop(session):
    decider = FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=100, reasoning_nl="x")
    )
    strategy = cross_strategy(stop_loss_pct=10, trailing_stop_pct=4, take_profit_pct=None)
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, strategy
    )
    await engine.run_cycle(session)
    pos = open_position(session, "paper", "BTC-EUR")
    assert pos.high_price == 12.0 and pos.stop_price == pytest.approx(12.0 * 0.96)


# -- follow-up after "wait for the turn", and consulting Claude by hand ---------------------


def dip_turn_strategy() -> Strategy:
    """Buy the turn while still in the dip: close below 10 and rising vs. the candle before."""
    return Strategy.model_validate(
        {
            "name": "Dip en draai",
            "market": "BTC-EUR",
            "interval": "1h",
            "indicators": [{"type": "sma", "id": "sma2", "period": 2}],
            "buy": [
                {"left": "close", "op": "<", "right": 10},
                {"left": "close", "op": "rising", "right": 1},
            ],
            "sell": [{"left": "close", "op": ">", "right": 20}],
            "stop_loss_pct": 10,
        }
    )


DIP_TURN = [12, 11, 10.5, 9.9, 9.8, 9.95]  # below 10 and rising: a buy signal
HOLD = TradeDecision(
    action="hold",
    confidence=0.6,
    amount_quote=0,
    reasoning_nl="Draai nog zwak.",
    revisit_next_cycle=True,
)


async def _held_to_wait_for_the_turn(session, decider):
    engine = make_engine_(
        session, {"BTC-EUR": candles(DIP_TURN)}, {"BTC-EUR": 9.95}, decider, dip_turn_strategy()
    )
    (result,) = await engine.run_cycle(session)
    assert result.signal == "buy" and result.action == "hold" and result.source == "claude"
    assert (
        decider.contexts[0]["follow_up"] is None and decider.contexts[0]["owner_request"] is False
    )
    assert list(engine.revisit) == ["BTC-EUR"] and engine.revisit["BTC-EUR"]["price"] == 9.95
    (status,) = engine.revisit_status()
    assert status["market"] == "BTC-EUR" and status["max_price"] == pytest.approx(10.149)
    return engine


async def test_claude_waiting_for_the_turn_gets_the_market_again_once_it_turns(session):
    decider = FakeDecider(HOLD)
    engine = await _held_to_wait_for_the_turn(session, decider)
    # Next cycle: the dip is over (close 10.05 is not below 10) but the turn holds and the
    # price is within 2% of the hold: a buy signal for Claude after all.
    engine.client.series["BTC-EUR"] = candles(DIP_TURN + [10.05])
    engine.client.prices["BTC-EUR"] = 10.05
    decider.decision = TradeDecision(
        action="buy", confidence=0.8, amount_quote=50, reasoning_nl="Draai bevestigd."
    )
    (result,) = await engine.run_cycle(session)
    assert result.signal == "buy" and result.action == "buy" and result.executed
    follow_up = decider.contexts[1]["follow_up"]
    assert follow_up["price_then"] == 9.95 and "koers stijgt" in follow_up["turn_now"]
    assert decider.contexts[1]["signal"] == "buy"
    assert engine.revisit == {}
    d = session.query(Decision).order_by(Decision.id.desc()).first()
    assert (
        d.reasoning.startswith("Vervolg: Claude wachtte om") and "Draai bevestigd." in d.reasoning
    )
    assert open_position(session, "paper", "BTC-EUR") is not None


async def test_follow_up_waits_for_the_turn_and_does_not_chase_a_price_that_ran_away(
    session,
):
    decider = FakeDecider(HOLD)
    engine = await _held_to_wait_for_the_turn(session, decider)
    # Turned, but the price is more than 2% above the hold: no chasing, Claude is not asked.
    engine.client.series["BTC-EUR"] = candles(DIP_TURN + [10.3])
    engine.client.prices["BTC-EUR"] = 10.3
    (result,) = await engine.run_cycle(session)
    assert result.signal == "hold" and result.source == "rules"
    assert len(decider.contexts) == 1 and "BTC-EUR" in engine.revisit
    # Falling again (and no dip, so the dip watch stays out of it): the follow-up waits.
    engine.client.series["BTC-EUR"] = candles(DIP_TURN + [10.3, 10.2])
    engine.client.prices["BTC-EUR"] = 10.2
    (result,) = await engine.run_cycle(session)
    assert result.signal == "hold" and result.source == "rules"
    assert len(decider.contexts) == 1 and "BTC-EUR" in engine.revisit and engine.watch == {}
    # After REVISIT_MAX_MIN the follow-up expires, even when the turn is there now.
    engine.revisit["BTC-EUR"]["since"] = datetime.now(UTC) - timedelta(minutes=181)
    engine.client.series["BTC-EUR"] = candles(DIP_TURN + [10.3, 10.0, 10.05])
    engine.client.prices["BTC-EUR"] = 10.05
    (result,) = await engine.run_cycle(session)
    assert result.source == "rules" and engine.revisit == {} and len(decider.contexts) == 1


async def test_follow_up_is_not_armed_without_the_flag_and_survives_a_restart(session):
    plain_hold = TradeDecision(
        action="hold", confidence=0.6, amount_quote=0, reasoning_nl="Nieuws."
    )
    decider = FakeDecider(plain_hold)
    engine = make_engine_(
        session, {"BTC-EUR": candles(DIP_TURN)}, {"BTC-EUR": 9.95}, decider, dip_turn_strategy()
    )
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and engine.revisit == {}
    decider.decision = HOLD
    await engine.run_cycle(session)
    assert "BTC-EUR" in engine.revisit
    engine.save_state(session)
    fresh = make_engine_(session, {"BTC-EUR": candles(DIP_TURN)}, {"BTC-EUR": 9.95}, decider)
    fresh.load_state(session)
    assert fresh.revisit["BTC-EUR"]["price"] == 9.95
    assert fresh.revisit["BTC-EUR"]["since"] == engine.revisit["BTC-EUR"]["since"]
    # An expired follow-up is not restored.
    engine.revisit["BTC-EUR"]["since"] -= timedelta(minutes=200)
    engine.save_state(session)
    fresh.load_state(session)
    assert fresh.revisit == {}


async def test_consult_asks_claude_without_a_signal_but_he_still_cannot_buy(session):
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=0.7, amount_quote=0, reasoning_nl="Geen dip.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    result = await engine.run_consult(session, "BTC-EUR")
    assert result.signal == "hold" and result.action == "hold" and result.source == "claude"
    assert decider.contexts[0]["owner_request"] is True and decider.contexts[0]["signal"] == "hold"
    (d,) = session.query(Decision).all()
    assert d.source == "claude" and d.reasoning == "Op verzoek beoordeeld. Geen dip."
    assert (
        engine.last_results[-1]["market"] == "BTC-EUR"
        and engine.last_results[-1]["source"] == "claude"
    )
    # The rules still hold: a buy without a signal is refused, whoever asked.
    decider.decision = TradeDecision(
        action="buy", confidence=1, amount_quote=50, reasoning_nl="Kopen."
    )
    result = await engine.run_consult(session, "BTC-EUR")
    assert result.action == "buy" and not result.executed and "zonder buy-signaal" in result.reason
    assert open_position(session, "paper", "BTC-EUR") is None
    with pytest.raises(ValueError, match="staat niet bij de markten"):
        await engine.run_consult(session, "ETH-EUR")
