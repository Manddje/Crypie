import pytest
from pydantic import ValidationError

from app.strategy.describe import explain_signal, signal_reason_nl
from app.strategy.engine import (
    SOFT_FAIL_SIZE_FACTOR,
    backtest,
    compute_series,
    latest_snapshot,
    signal_at,
)
from app.strategy.model import DEFAULT_STRATEGY, Strategy


def make_candles(closes, start_ts=1_700_000_000_000, step=3_600_000):
    out = []
    for i, c in enumerate(closes):
        out.append([start_ts + i * step, c, c * 1.01, c * 0.99, float(c), 1.0])
    return out


def crossover_strategy(**overrides):
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
    }
    base.update(overrides)
    return Strategy.model_validate(base)


def test_default_strategy_is_valid():
    assert DEFAULT_STRATEGY.series_names() >= {"rsi14", "ema200", "close"}


def test_unknown_series_reference_rejected():
    with pytest.raises(ValidationError, match="unknown series"):
        crossover_strategy(buy=[{"left": "nope", "op": ">", "right": 1}])


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="unique"):
        crossover_strategy(
            indicators=[
                {"type": "sma", "id": "x", "period": 2},
                {"type": "ema", "id": "x", "period": 3},
            ],
            buy=[{"left": "x", "op": ">", "right": 1}],
            sell=[{"left": "x", "op": "<", "right": 1}],
        )


def test_macd_series_exposed():
    s = crossover_strategy(
        indicators=[{"type": "macd", "id": "m"}],
        buy=[{"left": "m", "op": "crosses_above", "right": "m_signal"}],
        sell=[{"left": "m_hist", "op": "<", "right": 0}],
    )
    series = compute_series(s, make_candles([100 + i for i in range(50)]))
    assert {"m", "m_signal", "m_hist"} <= set(series)


def test_crossover_signals_and_backtest_round_trip():
    # Down, then sharply up (fast crosses above slow), then down again (crosses below).
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 15, 12, 9, 6, 5]
    candles = make_candles(closes)
    s = crossover_strategy()
    series = compute_series(s, candles)
    signals = [signal_at(s, series, i) for i in range(len(closes))]
    assert "buy" in signals and "sell" in signals
    assert signals.index("buy") < signals.index("sell")

    result = backtest(s, candles, start_eur=1000, fee_pct=0.0)
    assert len(result.closed_trades) >= 1
    first = result.closed_trades[0]
    assert first.reason == "signal"
    assert result.final_eur == pytest.approx(
        1000 * (first.exit_price / first.entry_price), rel=1e-6
    )
    summary = result.summary()
    assert summary["trades"] == 1 and summary["buy_hold_return_pct"] == -50.0


def test_stop_loss_triggers_at_threshold_with_fees():
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 10, 10, 10]
    candles = make_candles(closes)
    s = crossover_strategy(stop_loss_pct=10)
    result = backtest(s, candles, start_eur=1000, fee_pct=0.25)
    trade = result.closed_trades[0]
    assert trade.reason == "stop_loss"
    assert trade.exit_price == pytest.approx(trade.entry_price * 0.9)
    expected = 1000 * (1 - 0.0025) * 0.9 * (1 - 0.0025)
    assert result.final_eur == pytest.approx(expected)
    assert result.max_drawdown_pct > 0


def test_take_profit_triggers():
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 30, 30]
    s = crossover_strategy(take_profit_pct=20)
    result = backtest(s, make_candles(closes), fee_pct=0)
    assert result.closed_trades[0].reason == "take_profit"


def test_latest_snapshot_reports_signal_and_values():
    snap = latest_snapshot(crossover_strategy(), make_candles([1, 2, 3, 4, 5, 6]))
    assert snap["signal"] in {"buy", "sell", "hold"}
    assert "fast" in snap["values"] and "slow" in snap["values"]


def test_backtest_needs_candles():
    with pytest.raises(ValueError):
        backtest(crossover_strategy(), make_candles([1]))


def _with_soft_filter(strict: Strategy, right: float) -> Strategy:
    """``strict`` plus a soft trend filter: close above ``right``."""
    return Strategy.model_validate(
        {
            **strict.model_dump(),
            "buy": [
                *(c.model_dump() for c in strict.buy),
                {"left": "close", "op": ">", "right": right, "soft": True},
            ],
        }
    )


def test_soft_condition_does_not_block_the_signal_but_halves_the_backtest_position():
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 15, 12, 9, 6, 5]
    candles = make_candles(closes)
    strict = crossover_strategy()
    # A trend filter nothing in this series satisfies: close above a very high constant.
    soft = _with_soft_filter(strict, 1000)
    series = compute_series(soft, candles)
    live = [signal_at(soft, series, i) for i in range(len(closes))]
    strict_signals = [signal_at(soft, series, i, soft_as_hard=True) for i in range(len(closes))]
    assert "buy" in live and "buy" not in strict_signals

    # Like the live bot: the signal fires on the hard condition, the buy gets half the size.
    plain = backtest(strict, candles, fee_pct=0.0)
    halved = backtest(soft, candles, fee_pct=0.0)
    assert len(halved.trades) == len(plain.trades) >= 1
    assert all(t.size_factor == SOFT_FAIL_SIZE_FACTOR for t in halved.trades)
    assert halved.trades[0].soft_failed == ["close > 1000"]
    assert halved.trades[0].amount == pytest.approx(plain.trades[0].amount / 2)
    assert halved.return_pct == pytest.approx(plain.return_pct / 2)
    assert halved.summary()["soft_mode"] == "live"
    assert halved.summary()["half_size_trades"] == len(halved.trades)

    # The strict variant counts the filter as a hard condition: no trade at all.
    strict_result = backtest(soft, candles, fee_pct=0.0, soft_mode="strict")
    assert strict_result.summary()["trades"] == 0 and strict_result.soft_mode == "strict"

    # A soft filter that holds changes nothing.
    held = backtest(_with_soft_filter(strict, 0), candles, fee_pct=0.0)
    assert [t.size_factor for t in held.trades] == [1.0] * len(plain.trades)
    assert held.return_pct == pytest.approx(plain.return_pct)

    i = live.index("buy")
    checks = explain_signal(soft, series, i)
    assert [c.soft for c in checks["buy"]] == [False, True]
    assert checks["buy"][1].ok is False and checks["buy"][1].text.endswith("(zacht filter)")
    assert "1 zacht filter niet gehaald" in signal_reason_nl("buy", checks)

    # Only soft conditions: they count as hard, otherwise every candle would be a buy.
    all_soft = Strategy.model_validate(
        {**soft.model_dump(), "buy": [{"left": "close", "op": ">", "right": 1000, "soft": True}]}
    )
    series = compute_series(all_soft, candles)
    assert all(signal_at(all_soft, series, i) != "buy" for i in range(len(closes)))


def test_rising_condition_compares_with_its_own_past_value():
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 15, 12, 9, 6, 5]
    candles = make_candles(closes)
    s = Strategy.model_validate(
        {
            **crossover_strategy().model_dump(),
            "buy": [{"left": "close", "op": "rising", "right": 2}],
            "sell": [{"left": "close", "op": "falling", "right": 1}],
        }
    )
    series = compute_series(s, candles)
    signals = [signal_at(s, series, i) for i in range(len(closes))]
    # close rising vs 2 candles ago first at index 5 (9 > 7); falling vs 1 ago at index 1.
    assert signals[0] == "hold" and signals[1] == "sell" and signals[5] == "buy"
    checks = explain_signal(s, series, 6)
    assert checks["buy"][0].text == "koers stijgt t.o.v. 2 candles eerder"
    assert (checks["buy"][0].left, checks["buy"][0].right, checks["buy"][0].ok) == (12, 6, True)
    with pytest.raises(ValidationError):
        Strategy.model_validate(
            {**s.model_dump(), "buy": [{"left": "close", "op": "rising", "right": "ema50"}]}
        )
    with pytest.raises(ValidationError):
        Strategy.model_validate(
            {**s.model_dump(), "buy": [{"left": "close", "op": "rising", "right": 0}]}
        )


def test_drawdown_indicator_measures_discount_to_rolling_high():
    from app.indicators.core import drawdown

    assert drawdown([100, 110, 99, 88], 3) == [0.0, 0.0, 10.0, 20.0]
    s = Strategy.model_validate(
        {
            **crossover_strategy().model_dump(),
            "indicators": [{"type": "drawdown", "id": "dd3", "period": 3}],
            "buy": [{"left": "dd3", "op": ">=", "right": 15}],
            "sell": [{"left": "dd3", "op": "<", "right": 1}],
        }
    )
    series = compute_series(s, make_candles([100, 110, 99, 88]))
    assert signal_at(s, series, 3) == "buy" and signal_at(s, series, 1) == "sell"


def test_trailing_stop_follows_the_high_and_reports_its_own_reason():
    # Entry on the crossover at 12, rally to 20, then a dip to 18.4 (8% off the high).
    closes = [10, 9, 8, 7, 6, 9, 12, 15, 18, 20, 19.5, 18.4, 18, 18]
    s = crossover_strategy(stop_loss_pct=10, trailing_stop_pct=5)
    result = backtest(s, make_candles(closes), fee_pct=0)
    trade = result.closed_trades[0]
    assert trade.reason == "trailing_stop"
    # The peak is the high of the 20-candle (20 * 1.01); the stop sits 5% under it.
    assert trade.exit_price == pytest.approx(20 * 1.01 * 0.95)
    assert trade.exit_price > trade.entry_price  # a profit, where the plain stop-loss gave none


def test_trailing_stop_waits_for_the_start_threshold():
    closes = [10, 9, 8, 7, 6, 9, 12, 12.2, 11.7, 11.6, 11.6, 11.6]
    # Without a start threshold a 3% trail from the 12.2 high (12.32) fires at 11.95.
    s = crossover_strategy(stop_loss_pct=10, trailing_stop_pct=3)
    assert backtest(s, make_candles(closes), fee_pct=0).closed_trades[0].reason == "trailing_stop"
    # With trailing only after +5% the small rally never arms it: no trailing exit, the trade
    # ends on the later crossover signal instead.
    s = crossover_strategy(stop_loss_pct=10, trailing_stop_pct=3, trailing_start_pct=5)
    result = backtest(s, make_candles(closes), fee_pct=0)
    assert [t.reason for t in result.trades] == ["signal"]


def test_initial_and_trailing_stop_helpers():
    s = crossover_strategy(stop_loss_pct=10, trailing_stop_pct=3)
    assert s.initial_stop(100) == pytest.approx(97)  # the tighter of the two, trailing is live
    s = crossover_strategy(stop_loss_pct=10, trailing_stop_pct=3, trailing_start_pct=2)
    assert s.initial_stop(100) == pytest.approx(90)  # trailing not armed yet
    assert s.trailing_stop(101, 100) is None and s.trailing_stop(102, 100) == pytest.approx(98.94)
    assert crossover_strategy().initial_stop(100) is None
    assert DEFAULT_STRATEGY.trailing_stop_pct == 3 and DEFAULT_STRATEGY.trailing_start_pct == 2
    assert [c.op for c in DEFAULT_STRATEGY.sell] == [">", "falling"]


def _ohlc(rows):
    """Candles from (open, high, low, close) tuples, hourly."""
    return [
        [1_700_000_000_000 + i * 3_600_000, o, h, lo, c, 1.0]
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def test_fibonacci_levels_of_an_upward_swing():
    from app.indicators.core import fibonacci

    # Low 100, rally to high 200, then a pullback that closes at 150 (a 50% retracement).
    rows = [(100, 101, 100, 100), (120, 160, 110, 150), (150, 200, 150, 190), (190, 192, 149, 150)]
    fib = fibonacci(*[[r[k] for r in rows] for k in range(4)], period=4)
    assert fib["up"][3] == 1.0 and fib["high"][3] == 200 and fib["low"][3] == 100
    assert fib[""][3] == pytest.approx(50)
    assert fib["382"][3] == pytest.approx(161.8)
    assert fib["500"][3] == pytest.approx(150)
    assert fib["618"][3] == pytest.approx(138.2)
    assert fib["near"][3] == pytest.approx(0)
    assert fib[""][:3] == [None, None, None]  # window not full yet


def test_fibonacci_downward_swing_measures_the_bounce_from_the_low():
    from app.indicators.core import fibonacci

    # High 200 first, then a slide to 100 and a bounce that closes at 138.2 (38.2% back up).
    rows = [
        (200, 200, 190, 195),
        (190, 190, 150, 155),
        (150, 155, 100, 105),
        (105, 140, 104, 138.2),
    ]
    fib = fibonacci(*[[r[k] for r in rows] for k in range(4)], period=4)
    assert fib["up"][3] == 0.0
    assert fib[""][3] == pytest.approx(38.2)
    assert fib["382"][3] == pytest.approx(138.2)  # resistance, measured up from the low
    assert fib["618"][3] == pytest.approx(161.8)


def test_fibonacci_strategy_filters_the_golden_pocket():
    rows = [(100, 101, 100, 100), (120, 160, 110, 150), (150, 200, 150, 190), (190, 192, 149, 150)]
    s = Strategy.model_validate(
        {
            **crossover_strategy().model_dump(),
            "indicators": [{"type": "fibonacci", "id": "fib", "period": 4}],
            "buy": [
                {"left": "fib", "op": ">=", "right": 38.2},
                {"left": "fib", "op": "<=", "right": 61.8},
                {"left": "fib_up", "op": ">", "right": 0},
            ],
            "sell": [{"left": "fib", "op": "<", "right": 5}],
        }
    )
    assert {"fib", "fib_up", "fib_382", "fib_500", "fib_618", "fib_near"} <= s.series_names()
    series = compute_series(s, _ohlc(rows))
    assert signal_at(s, series, 3) == "buy"
    checks = explain_signal(s, series, 3)
    assert all(c.ok for c in checks["buy"])


def test_ema_support_reports_distance_and_a_held_test():
    from app.indicators.core import ema_support

    closes = [100.0] * 10
    highs = [101.0] * 10
    lows = [99.5] * 10
    lows[-1] = 99.2  # wick down through the EMA (~100.25), close back above it at 100.5
    closes[-1] = 100.5
    sup = ema_support(highs, lows, closes, period=3, tolerance_pct=1.0)
    assert sup["ema"][-1] == pytest.approx(100.25)
    assert sup[""][-1] == pytest.approx(100.5 / 100.25 * 100 - 100, abs=0.01)
    assert sup["bounce"][-1] == 1.0
    # A candle that closes far below the EMA did not hold the support.
    closes2 = [100.0] * 9 + [95.0]
    sup2 = ema_support(highs, [99.5] * 9 + [94.0], closes2, period=3, tolerance_pct=1.0)
    assert sup2["bounce"][-1] == 0.0 and sup2[""][-1] < -2


def test_ema_support_strategy_series_and_signal():
    s = Strategy.model_validate(
        {
            **crossover_strategy().model_dump(),
            "indicators": [{"type": "ema_support", "id": "e3", "period": 3, "tolerance_pct": 1}],
            "buy": [{"left": "e3_bounce", "op": ">", "right": 0}],
            "sell": [{"left": "e3", "op": "<", "right": -2}],
        }
    )
    assert {"e3", "e3_ema", "e3_bounce"} <= s.series_names()
    rows = [(100, 101, 99.5, 100)] * 5 + [(100, 101, 99.2, 100.5)] + [(100, 100, 94, 95)]
    series = compute_series(s, _ohlc(rows))
    assert signal_at(s, series, 5) == "buy"
    assert signal_at(s, series, 6) == "sell"


def test_levels_summary_describes_fibonacci_and_ema_support_in_dutch():
    from app.strategy.describe import levels_summary

    s = Strategy.model_validate(
        {
            **crossover_strategy().model_dump(),
            "indicators": [
                {"type": "fibonacci", "id": "fib", "period": 4},
                {"type": "ema_support", "id": "e3", "period": 3},
            ],
            "buy": [{"left": "fib", "op": ">=", "right": 38.2}],
            "sell": [{"left": "e3", "op": "<", "right": -3}],
        }
    )
    rows = [(100, 101, 100, 100), (120, 160, 110, 150), (150, 200, 150, 190), (190, 192, 149, 150)]
    series = compute_series(s, _ohlc(rows))
    values = {k: v[3] for k, v in series.items()}
    levels = levels_summary(s, values)
    assert [lv["type"] for lv in levels] == ["fibonacci", "ema_support"]
    fib = levels[0]
    assert fib["in_golden_pocket"] is True and fib["upward_swing"] is True
    assert "golden pocket" in fib["text_nl"] and "steunniveaus" in fib["text_nl"]
    assert "EMA3" in levels[1]["text_nl"]
    # Missing series (strategy changed since the journal entry) are skipped, not an error.
    assert levels_summary(s, {"fib": 50.0}) == []
