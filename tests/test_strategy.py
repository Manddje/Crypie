import pytest
from pydantic import ValidationError

from app.strategy.engine import backtest, compute_series, latest_snapshot, signal_at
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
    assert DEFAULT_STRATEGY.series_names() >= {"rsi14", "ema50", "close"}


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
