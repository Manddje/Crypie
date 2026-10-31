"""Turn a Strategy plus candles into indicator series, signals and a backtest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.indicators.core import Series, bollinger, ema, macd, rsi, sma
from app.strategy.model import Condition, Strategy

Candle = list[float]  # [timestamp_ms, open, high, low, close, volume]
Signal = Literal["buy", "sell", "hold"]


def compute_series(strategy: Strategy, candles: list[Candle]) -> dict[str, Series]:
    closes = [c[4] for c in candles]
    out: dict[str, Series] = {
        "open": [c[1] for c in candles],
        "high": [c[2] for c in candles],
        "low": [c[3] for c in candles],
        "close": closes,
        "volume": [c[5] for c in candles],
    }
    for ind in strategy.indicators:
        if ind.type == "sma":
            out[ind.id] = sma(closes, ind.period)
        elif ind.type == "ema":
            out[ind.id] = ema(closes, ind.period)
        elif ind.type == "rsi":
            out[ind.id] = rsi(closes, ind.period)
        elif ind.type == "macd":
            line, sig, hist = macd(closes, ind.fast, ind.slow, ind.signal)
            out[ind.id], out[f"{ind.id}_signal"], out[f"{ind.id}_hist"] = line, sig, hist
        elif ind.type == "bollinger":
            up, mid, low = bollinger(closes, ind.period, ind.std_dev)
            out[f"{ind.id}_upper"], out[f"{ind.id}_middle"], out[f"{ind.id}_lower"] = up, mid, low
    return out


def _value(series: dict[str, Series], ref: str | float, i: int) -> float | None:
    if isinstance(ref, str):
        return series[ref][i]
    return float(ref)


def check_condition(cond: Condition, series: dict[str, Series], i: int) -> bool:
    left = _value(series, cond.left, i)
    right = _value(series, cond.right, i)
    if left is None or right is None:
        return False
    if cond.op == "<":
        return left < right
    if cond.op == "<=":
        return left <= right
    if cond.op == ">":
        return left > right
    if cond.op == ">=":
        return left >= right
    # crosses: compare with the previous bar
    if i == 0:
        return False
    prev_left = _value(series, cond.left, i - 1)
    prev_right = _value(series, cond.right, i - 1)
    if prev_left is None or prev_right is None:
        return False
    if cond.op == "crosses_above":
        return prev_left <= prev_right and left > right
    return prev_left >= prev_right and left < right


def hard_conditions(conditions: list[Condition]) -> list[Condition]:
    """The conditions that must hold for a signal; all of them when none is marked hard."""
    hard = [c for c in conditions if not c.soft]
    return hard or list(conditions)


def signal_at(
    strategy: Strategy, series: dict[str, Series], i: int, soft_as_hard: bool = False
) -> Signal:
    """Strategy signal on candle ``i``.

    Live, soft conditions do not block the signal (Claude weighs them). A backtest has no
    Claude, so it passes ``soft_as_hard=True`` and simulates the strict version.
    """
    buy = strategy.buy if soft_as_hard else hard_conditions(strategy.buy)
    sell = strategy.sell if soft_as_hard else hard_conditions(strategy.sell)
    if all(check_condition(c, series, i) for c in buy):
        return "buy"
    if all(check_condition(c, series, i) for c in sell):
        return "sell"
    return "hold"


def latest_snapshot(strategy: Strategy, candles: list[Candle]) -> dict[str, object]:
    """Signal and indicator values for the most recent candle (used by the dashboard)."""
    if not candles:
        return {"signal": "hold", "values": {}}
    series = compute_series(strategy, candles)
    i = len(candles) - 1
    values = {k: round(v[i], 6) for k, v in series.items() if k != "volume" and v[i] is not None}
    return {"signal": signal_at(strategy, series, i), "values": values, "timestamp": candles[i][0]}


@dataclass
class Trade:
    entry_time: int
    entry_price: float
    exit_time: int | None = None
    exit_price: float | None = None
    amount: float = 0.0
    reason: str = ""

    @property
    def pnl_pct(self) -> float | None:
        if self.exit_price is None:
            return None
        return (self.exit_price / self.entry_price - 1) * 100


@dataclass
class BacktestResult:
    start_eur: float
    final_eur: float
    return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    trades: list[Trade] = field(default_factory=list)
    candles: int = 0
    fee_pct: float = 0.0

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.exit_price is not None]

    @property
    def win_rate_pct(self) -> float | None:
        closed = self.closed_trades
        if not closed:
            return None
        wins = sum(1 for t in closed if (t.pnl_pct or 0) > 0)
        return wins / len(closed) * 100

    def summary(self) -> dict[str, object]:
        return {
            "candles": self.candles,
            "start_eur": round(self.start_eur, 2),
            "final_eur": round(self.final_eur, 2),
            "return_pct": round(self.return_pct, 2),
            "buy_hold_return_pct": round(self.buy_hold_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "trades": len(self.closed_trades),
            "open_position": any(t.exit_price is None for t in self.trades),
            "win_rate_pct": round(self.win_rate_pct, 1) if self.win_rate_pct is not None else None,
            "fee_pct": self.fee_pct,
        }


def backtest(
    strategy: Strategy,
    candles: list[Candle],
    start_eur: float = 1000.0,
    fee_pct: float = 0.25,
) -> BacktestResult:
    """Long-only backtest. Signals on bar i are executed at the close of bar i.

    Soft conditions count as hard here: without Claude the strict rules are simulated.

    Stop-loss and take-profit are checked against the bar's low/high and filled at the
    threshold price. Fees are charged on both entry and exit.
    """
    if len(candles) < 2:
        raise ValueError("need at least 2 candles")
    series = compute_series(strategy, candles)
    fee = fee_pct / 100.0
    eur = start_eur
    coin = 0.0
    trades: list[Trade] = []
    open_trade: Trade | None = None
    peak = start_eur
    max_dd = 0.0

    for i, candle in enumerate(candles):
        ts, _o, high, low, close, _v = candle

        if open_trade is not None:
            exit_price = None
            reason = ""
            if strategy.stop_loss_pct is not None:
                stop = open_trade.entry_price * (1 - strategy.stop_loss_pct / 100)
                if low <= stop:
                    exit_price, reason = stop, "stop_loss"
            if exit_price is None and strategy.take_profit_pct is not None:
                target = open_trade.entry_price * (1 + strategy.take_profit_pct / 100)
                if high >= target:
                    exit_price, reason = target, "take_profit"
            if exit_price is None and signal_at(strategy, series, i, soft_as_hard=True) == "sell":
                exit_price, reason = close, "signal"
            if exit_price is not None:
                eur += coin * exit_price * (1 - fee)
                coin = 0.0
                open_trade.exit_time, open_trade.exit_price = int(ts), exit_price
                open_trade.reason = reason
                open_trade = None
        elif signal_at(strategy, series, i, soft_as_hard=True) == "buy":
            spend = eur * strategy.position_size_pct / 100
            if spend > 0:
                coin = spend * (1 - fee) / close
                eur -= spend
                open_trade = Trade(entry_time=int(ts), entry_price=close, amount=coin)
                trades.append(open_trade)

        equity = eur + coin * close
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    final = eur + coin * candles[-1][4]
    first_close, last_close = candles[0][4], candles[-1][4]
    return BacktestResult(
        start_eur=start_eur,
        final_eur=final,
        return_pct=(final / start_eur - 1) * 100,
        buy_hold_return_pct=(last_close / first_close - 1) * 100,
        max_drawdown_pct=max_dd,
        trades=trades,
        candles=len(candles),
        fee_pct=fee_pct,
    )
