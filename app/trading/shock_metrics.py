"""Is the market still in shock? The numbers behind the shock assessment, per coin.

Pure functions on Bitvavo 1h candles ``[timestamp_ms, open, high, low, close, volume]`` (oldest
first, UTC). Everything that cannot be computed is ``None``, shown to Claude as "onbekend";
never 0, because a 0 reads as "no movement" or "no volume" and that is a claim.

* Abnormality: how big the last three hourly moves and volumes are against the median of the
  168 hours (7 days) before the newest candle. The newest candle is the running one; its volume
  is scaled to a full hour by the minutes elapsed.
* Since the first report: the price when the fact first appeared, the lowest price since (and
  when), the price now, the drop, how much of the drop has been recovered, and whether the low
  is less than two hours old.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

HOUR_MS = 3_600_000
BASELINE_CANDLES = 168  # 7 days of hourly candles
MIN_BASELINE = 24  # fewer known hours in the baseline window: the ratio is not known
ABNORMAL_CANDLES = 3  # the newest three hours, newest first
MIN_ELAPSED_MIN = 5.0  # a running candle younger than this is scaled as if it were 5 min old
NEW_LOW_WINDOW = timedelta(hours=2)
UNKNOWN = "onbekend"


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _slot(ms: int) -> int:
    return ms - ms % HOUR_MS


def _at(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _valid(row: Any) -> bool:
    try:
        return len(row) >= 6 and all(float(x) == float(x) for x in row[:6]) and float(row[1]) > 0
    except (TypeError, ValueError):
        return False


def by_slot(candles: list[list[float]], now: datetime) -> dict[int, list[float]]:
    """Candles per hour start, without rows from the future or rows that do not parse."""
    limit = _ms(now)
    out: dict[int, list[float]] = {}
    for row in candles or []:
        if not _valid(row):
            continue
        start = _slot(int(row[0]))
        if start <= limit:
            out[start] = [float(x) for x in row[:6]]
    return out


def _move(row: list[float]) -> float:
    return abs(row[4] - row[1]) / row[1]


def abnormality(candles: list[list[float]], now: datetime) -> dict[str, list[float | None]]:
    """Move and volume of the newest three hours against their 7-day median, newest first.

    ``move``: |close - open| / open of the hour, divided by the median of the same over the 168
    hours before the newest (running) hour. ``volume``: the hour's volume divided by the median
    volume; the running hour's volume is scaled up by the minutes elapsed. A missing hour, or a
    baseline with fewer than MIN_BASELINE known hours or a median of 0, gives None.
    """
    rows = by_slot(candles, now)
    current = _slot(_ms(now))
    base = [
        rows[s] for s in range(current - BASELINE_CANDLES * HOUR_MS, current, HOUR_MS) if s in rows
    ]
    move_median = statistics.median(_move(r) for r in base) if len(base) >= MIN_BASELINE else 0
    volume_median = statistics.median(r[5] for r in base) if len(base) >= MIN_BASELINE else 0
    elapsed_min = max((_ms(now) - current) / 60_000, MIN_ELAPSED_MIN)
    moves: list[float | None] = []
    volumes: list[float | None] = []
    for k in range(ABNORMAL_CANDLES):
        slot = current - k * HOUR_MS
        row = rows.get(slot)
        if row is None:
            moves.append(None)
            volumes.append(None)
            continue
        moves.append(round(_move(row) / move_median, 2) if move_median > 0 else None)
        volume = row[5] * 60 / min(elapsed_min, 60) if k == 0 else row[5]
        volumes.append(round(volume / volume_median, 2) if volume_median > 0 else None)
    return {"move": moves, "volume": volumes}


def since_first_seen(
    candles: list[list[float]],
    now: datetime,
    first_seen: datetime,
    price_at: float | None = None,
    price_now: float | None = None,
) -> dict[str, Any]:
    """The price path since a fact first appeared.

    ``price_at`` is the stored price at first sight; without one, the open of the hour of the
    first sighting. ``price_now`` is the live price; without one, the last close. The low is
    the lowest candle low since the hour of the first sighting (or the live price, if lower);
    unknown when the candles do not reach back that far.
    """
    rows = by_slot(candles, now)
    start = _slot(_ms(first_seen))
    span = sorted(s for s in rows if s >= start)
    out: dict[str, Any] = {
        "price_at_first_seen": None,
        "low": None,
        "low_at": None,
        "price_now": None,
        "drop_pct": None,
        "change_pct": None,
        "recovery_pct": None,
        "new_low_last_2h": None,
    }
    covered = bool(rows) and min(rows) <= start
    if price_at is None and start in rows:
        price_at = rows[start][1]
    if price_now is None and span:
        price_now = rows[span[-1]][4]
    out["price_at_first_seen"] = price_at
    out["price_now"] = price_now
    if covered and span:
        low_slot = min(span, key=lambda s: (rows[s][3], s))  # the first time it got there
        low, low_at = rows[low_slot][3], _at(low_slot)
        if price_now is not None and price_now < low:
            low, low_at = price_now, now
        out["low"], out["low_at"] = low, low_at
    low = out["low"]
    if price_at:
        if price_now is not None:
            out["change_pct"] = round((price_now / price_at - 1) * 100, 2)
        if low is not None:
            out["drop_pct"] = round((low / price_at - 1) * 100, 2)
            if low < price_at and price_now is not None:
                out["recovery_pct"] = round((price_now - low) / (price_at - low) * 100, 1)
                out["new_low_last_2h"] = out["low_at"] >= now - NEW_LOW_WINDOW
            else:
                out["new_low_last_2h"] = False
    return out


# -- formatting for the prompts -----------------------------------------------------------


def fmt_price(value: float | None) -> str:
    if value is None:
        return UNKNOWN
    if value >= 1000:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.2f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def fmt_pct(value: float | None, digits: int = 1) -> str:
    return UNKNOWN if value is None else f"{value:+.{digits}f}%"


def fmt_ratio(value: float | None) -> str:
    return UNKNOWN if value is None else f"{value:.1f}x"


def fmt_hm(value: datetime | None) -> str:
    return UNKNOWN if value is None else value.astimezone(UTC).strftime("%H:%M UTC")


def fmt_bool(value: bool | None) -> str:
    return UNKNOWN if value is None else ("ja" if value else "nee")
