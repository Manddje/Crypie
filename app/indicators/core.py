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
