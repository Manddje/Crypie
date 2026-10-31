"""Long-term trend context per market, in the spirit of PlanB's monthly/weekly indicators.

This is deliberately *not* a trade signal: a 200-week average and a monthly RSI change sides a
few times a year at most. They tell the bot what kind of market it is trading in, so Claude can
be bolder with dips in a long uptrend and more careful in a long downtrend.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.db import Setting
from app.indicators.core import rsi, sma

log = logging.getLogger(__name__)

TREND_KEY_PREFIX = "trend:"
WEEKLY_PERIOD = 200  # the classic 200-week moving average
MONTHLY_RSI_PERIOD = 14
RSI_MIDLINE = 50.0  # PlanB: monthly RSI above 50 = uptrend, below = downtrend

REGIME_NL = {
    "up": "opwaarts",
    "down": "neerwaarts",
    "mixed": "gemengd",
    "unknown": "onbekend",
}


def regime_of(above_wma: bool | None, rsi_above: bool | None) -> str:
    if above_wma is None and rsi_above is None:
        return "unknown"
    known = [v for v in (above_wma, rsi_above) if v is not None]
    if all(known):
        return "up"
    if not any(known):
        return "down"
    return "mixed"


def trend_from_candles(
    market: str, weekly: list[list[float]], monthly: list[list[float]], price: float | None = None
) -> dict[str, Any]:
    """Compute the long-term trend from weekly and monthly candles (oldest first).

    The last candle of each series is the one still forming, so the values are "live" rather
    than month-end values; that is fine for context that only moves slowly.
    """
    weekly_closes = [c[4] for c in weekly]
    monthly_closes = [c[4] for c in monthly]
    last_price = price if price is not None else (weekly_closes[-1] if weekly_closes else None)

    wma200 = sma(weekly_closes, WEEKLY_PERIOD)[-1] if len(weekly_closes) >= WEEKLY_PERIOD else None
    above_wma = None if (wma200 is None or last_price is None) else last_price > wma200
    monthly_rsi = (
        rsi(monthly_closes, MONTHLY_RSI_PERIOD)[-1]
        if len(monthly_closes) > MONTHLY_RSI_PERIOD
        else None
    )
    rsi_above = None if monthly_rsi is None else monthly_rsi >= RSI_MIDLINE
    regime = regime_of(above_wma, rsi_above)

    return {
        "market": market,
        "price": last_price,
        "wma200": None if wma200 is None else round(wma200, 6),
        "weeks": len(weekly_closes),
        "above_wma200": above_wma,
        "vs_wma200_pct": None
        if (wma200 is None or last_price is None or wma200 == 0)
        else round((last_price / wma200 - 1) * 100, 1),
        "monthly_rsi": None if monthly_rsi is None else round(monthly_rsi, 1),
        "months": len(monthly_closes),
        "monthly_rsi_above_50": rsi_above,
        "regime": regime,
        "regime_nl": REGIME_NL[regime],
        "summary_nl": summary_nl(above_wma, wma200, monthly_rsi, regime, len(weekly_closes)),
        "updated_at": datetime.now(UTC).isoformat(),
        "note": "lange-termijncontext (PlanB-stijl), geen koop- of verkoopsignaal",
    }


def summary_nl(
    above_wma: bool | None,
    wma200: float | None,
    monthly_rsi: float | None,
    regime: str,
    weeks: int,
) -> str:
    parts: list[str] = []
    if above_wma is None:
        parts.append(
            f"te weinig historie voor het 200-weeks gemiddelde ({weeks} van {WEEKLY_PERIOD} weken)"
        )
    else:
        parts.append(("boven" if above_wma else "onder") + " het 200-weeks gemiddelde")
    if monthly_rsi is None:
        parts.append("te weinig maanden voor de maand-RSI")
    else:
        parts.append(
            f"maand-RSI {monthly_rsi:.0f} ({'boven' if monthly_rsi >= RSI_MIDLINE else 'onder'} 50)"
        )
    return ", ".join(parts) + f"; lange trend {REGIME_NL[regime]}"


async def compute_trend(client, market: str, price: float | None = None) -> dict[str, Any]:
    weekly = await client.candles(market, "1W", WEEKLY_PERIOD + 10)
    monthly = await client.candles(market, "1M", 60)
    return trend_from_candles(market, weekly, monthly, price)


def save_trend(session: Session, trend: dict[str, Any]) -> None:
    key = TREND_KEY_PREFIX + trend["market"]
    row = session.get(Setting, key)
    payload = json.dumps(trend)
    if row is None:
        session.add(Setting(key=key, value=payload))
    else:
        row.value = payload
    session.commit()


def load_trend(session: Session, market: str) -> dict[str, Any] | None:
    row = session.get(Setting, TREND_KEY_PREFIX + market)
    return json.loads(row.value) if row else None


def load_trends(session: Session, markets: list[str]) -> list[dict[str, Any]]:
    out = []
    for market in markets:
        trend = load_trend(session, market)
        if trend:
            out.append(trend)
    return out


async def refresh_trends(client, session: Session, markets: list[str]) -> list[dict[str, Any]]:
    """Recompute and store the trend for every market; one failing market does not stop the rest."""
    out = []
    for market in markets:
        try:
            trend = await compute_trend(client, market)
        except Exception as exc:  # network or market not listed; keep the previous value
            log.warning("trend for %s not refreshed: %s", market, exc)
            continue
        save_trend(session, trend)
        out.append(trend)
    return out
