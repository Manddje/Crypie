"""Technical indicators as pure functions over lists of floats.

Every function returns a list aligned with the input (same length). Positions where the
indicator is not yet defined (warm-up period) hold ``None``. Pure Python on purpose: the
inputs are at most a few thousand candles, and no numpy/pandas keeps the image small.
"""

from __future__ import annotations

from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], period: int) -> Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= period:
            window_sum -= values[i - period]
        if i >= period - 1:
            out[i] = window_sum / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period  # seed with SMA
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Wilder's RSI (0-100)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period

    def to_rsi(g: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        rs = g / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = to_rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = to_rsi(avg_gain, avg_loss)
    return out


def macd(
    values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """Return (macd_line, signal_line, histogram)."""
    if not fast < slow:
        raise ValueError("fast period must be smaller than slow period")
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: Series = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow, strict=True)
    ]
    defined = [v for v in line if v is not None]
    sig_defined = ema(defined, signal) if defined else []
    sig: Series = [None] * len(values)
    offset = len(values) - len(defined)
    for j, v in enumerate(sig_defined):
        sig[offset + j] = v
    hist: Series = [
        (m - s) if m is not None and s is not None else None for m, s in zip(line, sig, strict=True)
    ]
    return line, sig, hist


def bollinger(
    values: Sequence[float], period: int = 20, std_dev: float = 2.0
) -> tuple[Series, Series, Series]:
    """Return (upper, middle, lower) bands."""
    middle = sma(values, period)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = middle[i]
        assert mean is not None
        var = sum((x - mean) ** 2 for x in window) / period
        sd = var**0.5
        upper[i] = mean + std_dev * sd
        lower[i] = mean - std_dev * sd
    return upper, middle, lower


def drawdown(values: Sequence[float], period: int) -> Series:
    """Percent below the highest value of the last ``period`` values (0 = at the high).

    A cheap "how far has it fallen from its recent high" measure: ``drawdown(closes, 168)``
    on hourly candles is the discount to the 7-day high.
    """
    out: Series = []
    for i, v in enumerate(values):
        window = values[max(0, i - period + 1) : i + 1]
        high = max(window)
        out.append(round((1 - v / high) * 100, 6) if high else None)
    return out


FIB_LEVELS = (0.382, 0.5, 0.618)


def fibonacci(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> dict[str, Series]:
    """Fibonacci retracement of the latest swing in a rolling window of ``period`` candles.

    The swing runs from the lowest low to the highest high of the window (or the other way
    round); whichever extreme came last marks the end of the swing. Returned series:

    - ``""``: how far the price has retraced the swing, in percent of the swing (0 = still at
      the swing's end, 38.2 / 50 / 61.8 = at the classic levels, 100 = back at its start).
    - ``"up"``: 1 when the swing is upward (the high came after the low, the price is pulling
      back towards support), 0 when it is downward (the price bounces towards resistance).
    - ``"high"`` / ``"low"``: the swing high and low.
    - ``"382"`` / ``"500"`` / ``"618"``: the price at each level (support in an upward swing,
      resistance in a downward one).
    - ``"near"``: distance from the close to the nearest level, in percent of the price.

    Candles before the window is full hold ``None``.
    """
    if period < 2:
        raise ValueError("period must be >= 2")
    n = len(closes)
    keys = ("", "up", "high", "low", "382", "500", "618", "near")
    out: dict[str, Series] = {k: [None] * n for k in keys}
    for i in range(period - 1, n):
        start = i - period + 1
        hi_idx = start
        lo_idx = start
        for j in range(start, i + 1):
            if highs[j] >= highs[hi_idx]:
                hi_idx = j
            if lows[j] <= lows[lo_idx]:
                lo_idx = j
        high, low = highs[hi_idx], lows[lo_idx]
        span = high - low
        if span <= 0:
            continue
        if hi_idx == lo_idx:
            up = closes[hi_idx] >= opens[hi_idx]
        else:
            up = hi_idx > lo_idx
        close = min(max(closes[i], low), high)
        if up:
            retrace = (high - close) / span * 100
            levels = [high - k * span for k in FIB_LEVELS]
        else:
            retrace = (close - low) / span * 100
            levels = [low + k * span for k in FIB_LEVELS]
        out[""][i] = round(retrace, 4)
        out["up"][i] = 1.0 if up else 0.0
        out["high"][i] = high
        out["low"][i] = low
        out["382"][i], out["500"][i], out["618"][i] = (round(v, 8) for v in levels)
        out["near"][i] = round(min(abs(closes[i] / lv - 1) for lv in levels) * 100, 4)
    return out


def ema_support(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
    tolerance_pct: float = 1.0,
) -> dict[str, Series]:
    """An EMA as dynamic support: where the price sits relative to it and whether it held.

    - ``""``: distance of the close to the EMA in percent (positive = above the EMA).
    - ``"ema"``: the EMA itself.
    - ``"bounce"``: 1 when this candle tested the EMA as support and it held: the low came
      within ``tolerance_pct`` of the EMA (or dipped under it) while the close stayed above
      the EMA or at most ``tolerance_pct`` under it. Otherwise 0.
    """
    line = ema(closes, period)
    n = len(closes)
    out: dict[str, Series] = {k: [None] * n for k in ("", "ema", "bounce")}
    tol = tolerance_pct / 100
    for i, e in enumerate(line):
        if e is None or e <= 0:
            continue
        out[""][i] = round((closes[i] / e - 1) * 100, 4)
        out["ema"][i] = e
        tested = lows[i] <= e * (1 + tol)
        held = closes[i] >= e * (1 - tol)
        out["bounce"][i] = 1.0 if tested and held else 0.0
    return out
