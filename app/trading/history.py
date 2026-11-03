"""Data behind the charts on the overview page.

Two things are drawn: the price of one market as candles with the bot's actions on top
(buys, sells and the Claude advice that did not lead to an order), and the value of the
whole portfolio over time. The portfolio value is not stored anywhere, so it is rebuilt
from the current balance by walking the order history backwards: before a buy the wallet
held more euro and fewer coins, before a sell the other way round.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import Decision, Order
from app.labels import CLOSE_REASONS_NL

# Period shown on the chart -> (candle interval, number of candles). Each period covers
# exactly the number of candles given, so Bitvavo is asked for just enough.
PERIODS: dict[str, tuple[str, int]] = {
    "1d": ("15m", 96),
    "7d": ("1h", 168),
    "30d": ("4h", 180),
    "90d": ("1d", 90),
}
DEFAULT_PERIOD = "7d"
# Orders that moved no coins: refused, still in flight or with an unknown outcome. A
# cancelled order that filled nothing has amount 0 and is skipped by the amount filter.
NOT_FILLED = ("rejected", "pending", "unknown")
# Old rows may lack the amount; they keep showing as before.
_FILLED = (
    Order.status.not_in(NOT_FILLED),
    or_(Order.amount.is_(None), Order.amount > 0),
)

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}

# Why a position was closed -> the label in the chart's tooltip and the list below it, in the
# same words as the journal (app.labels.CLOSE_REASONS_NL): 'Verkocht: stop-loss geraakt'.
SELL_REASONS_NL: dict[str, str] = {
    reason: (phrase[:1].upper() + phrase[1:] if "verkocht" in phrase else f"Verkocht: {phrase}")
    for reason, phrase in CLOSE_REASONS_NL.items()
    if reason != "reset"
}
BUY_SOURCES_NL: dict[str, str] = {
    "claude": "Gekocht op advies van Claude",
    "rules": "Gekocht op strategiesignaal",
    "manual": "Handmatig gekocht",
}
ACTIONS_NL: dict[str, str] = {"buy": "kopen", "sell": "verkopen", "hold": "afwachten"}
# Marker text on the chart has room for one word.
SELL_SHORT_NL: dict[str, str] = {
    "stop_loss": "Stop-loss",
    "trailing_stop": "Trailing",
    "take_profit": "Take-profit",
    "signal": "Verkoop",
    "news_exit": "Nieuws",
    "manual": "Verkoop",
}


def period_spec(period: str | None) -> tuple[str, str, int]:
    """Normalise the period query parameter: (period, interval, limit)."""
    key = period if period in PERIODS else DEFAULT_PERIOD
    interval, limit = PERIODS[key]
    return key, interval, limit


def period_start(interval: str, limit: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now - timedelta(seconds=INTERVAL_SECONDS[interval] * limit)


def bar_time(moment: datetime, interval: str) -> int:
    """Epoch seconds of the candle that contains ``moment`` (candles start on the grid)."""
    step = INTERVAL_SECONDS[interval]
    seconds = int(moment.timestamp())
    return seconds - seconds % step


def candle_dicts(rows: list[list[float]]) -> list[dict]:
    """Bitvavo rows [ms, open, high, low, close, volume] -> chart points (epoch seconds)."""
    return [
        {
            "time": int(r[0]) // 1000,
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in rows
    ]


def _order_action(o: Order, interval: str) -> dict:
    reason = o.reason or ""
    if o.side == "buy":
        source = reason.split(":", 1)[0].strip() if reason else "manual"
        kind = "buy"
        label = BUY_SOURCES_NL.get(source, "Gekocht")
        short = "Koop"
        detail = reason.split(":", 1)[1].strip() if ":" in reason else ""
    else:
        source = "manual" if reason == "manual" else "bot"
        kind = "sell"
        label = SELL_REASONS_NL.get(reason, "Verkocht")
        short = SELL_SHORT_NL.get(reason, "Verkoop")
        detail = ""
    return {
        "id": f"order-{o.id}",
        "time": int(o.created_at.timestamp()),
        "bar_time": bar_time(o.created_at, interval),
        "kind": kind,
        "market": o.market,
        "price": o.price,
        "amount_quote": o.amount_quote,
        "amount": o.amount,
        "source": source,
        "reason": reason,
        "label": label,
        "short": short,
        "detail": detail,
        "executed": True,
    }


def _blocked_label(d: Decision) -> str:
    """'Kopen geblokkeerd': what the rule stopped. A decision blocked before Claude was asked
    (risk-off, an order underway, an invalid strategy) is stored as 'hold'; 'Afwachten
    geblokkeerd' would say the opposite of what happened, so name the order it prevented."""
    action = d.action
    if action == "hold":
        action = "sell" if d.signal == "sell" and d.has_position else "buy"
    return f"{ACTIONS_NL.get(action, action).capitalize()} geblokkeerd"


def _blocked_detail(d: Decision) -> str:
    """The rule, and for a risk-off also which one: the journal only stores 'risk-off', the
    reasoning carries the event or news shock and until when buying is off."""
    detail = d.blocked_by or ""
    reasoning = d.reasoning or ""
    if detail.lower().startswith("risk-off"):
        for marker in ("Risk-off: ", "(risk-off: "):
            at = reasoning.find(marker)
            if at >= 0:
                rest = reasoning[at + len(marker) :]
                end = rest.find(")") if marker.startswith("(") else rest.find(". ")
                return "Risk-off: " + (rest if end < 0 else rest[:end]).rstrip(".")
    return detail


def _decision_action(d: Decision, interval: str) -> dict:
    action_nl = ACTIONS_NL.get(d.action, d.action)
    if d.blocked_by:
        kind = "blocked"
        label = _blocked_label(d)
        short = "Geblokkeerd"
        detail = _blocked_detail(d)
    else:
        kind = "advice"
        label = f"Claude adviseert {action_nl}"
        short = "Claude"
        detail = d.reasoning or ""
    return {
        "id": f"decision-{d.id}",
        "time": int(d.created_at.timestamp()),
        "bar_time": bar_time(d.created_at, interval),
        "kind": kind,
        "market": d.market,
        "price": d.price,
        "amount_quote": d.amount_quote,
        "amount": None,
        "source": d.source,
        "reason": d.action,
        "label": label,
        "short": short,
        "detail": detail,
        "confidence": d.confidence,
        "executed": False,
    }


def market_actions(
    session: Session, mode: str, market: str, since: datetime, interval: str
) -> list[dict]:
    """Everything the bot did or considered in one market since ``since``, oldest first.

    Executed orders always count. Of the journal only the moments where Claude was asked,
    or where a buy or sell was blocked by a rule, are shown; the routine "hold" of every
    cycle would bury the chart in dots.
    """
    orders = session.scalars(
        select(Order)
        .where(Order.mode == mode, Order.market == market, Order.created_at >= since)
        .where(*_FILLED)
        .order_by(Order.created_at)
    )
    decisions = session.scalars(
        select(Decision)
        .where(Decision.mode == mode, Decision.market == market, Decision.created_at >= since)
        .where(Decision.executed.is_(False))
        .order_by(Decision.created_at)
    )
    out = [_order_action(o, interval) for o in orders]
    for d in decisions:
        if d.source == "claude" or d.blocked_by:
            out.append(_decision_action(d, interval))
    out.sort(key=lambda a: a["time"])
    return out


def trade_actions(session: Session, mode: str, since: datetime, interval: str) -> list[dict]:
    """Executed buys and sells in every market since ``since``, oldest first."""
    orders = session.scalars(
        select(Order)
        .where(Order.mode == mode, Order.created_at >= since)
        .where(*_FILLED)
        .order_by(Order.created_at)
    )
    return [_order_action(o, interval) for o in orders]


def portfolio_history(
    session: Session,
    mode: str,
    balances: dict[str, float],
    candles_by_market: dict[str, list[dict]],
    since: datetime,
) -> list[dict]:
    """Value of the portfolio (euro plus coins at the close of each candle) over time.

    Starts from today's balance and undoes the orders one by one going back in time, so no
    history table is needed. Deposits and withdrawals on a live account are not orders and
    therefore show up as a jump at the start of the period.
    """
    times = sorted({c["time"] for rows in candles_by_market.values() for c in rows})
    if not times:
        return []
    orders = list(
        session.scalars(
            select(Order)
            .where(Order.mode == mode, Order.created_at >= since)
            .where(*_FILLED)
            .order_by(Order.created_at.desc())
        )
    )
    # Holdings right now, then rewound to the moment of each candle.
    holdings = {k: float(v) for k, v in balances.items()}
    closes: dict[str, dict[int, float]] = {
        m: {c["time"]: c["close"] for c in rows} for m, rows in candles_by_market.items()
    }
    last_close: dict[str, float | None] = dict.fromkeys(candles_by_market)
    points: list[dict] = []
    pending = orders  # newest first; popped from the front while walking back in time
    # Walk the candles from newest to oldest so the rewind is a single pass.
    for t in reversed(times):
        while pending and int(pending[0].created_at.timestamp()) > t:
            _undo(holdings, pending.pop(0))
        value = holdings.get("EUR", 0.0)
        for market, series in closes.items():
            coin = market.split("-")[0]
            amount = holdings.get(coin, 0.0)
            if not amount:
                continue
            close = series.get(t)
            if close is None:
                # Candle missing for this market at this moment: reuse the nearest later one.
                close = last_close.get(market)
                if close is None:
                    continue
            last_close[market] = close
            value += amount * close
        points.append({"time": t, "value": round(value, 2)})
    points.reverse()
    return points


def _undo(holdings: dict[str, float], o: Order) -> None:
    base, quote = o.market.split("-")
    amount = o.amount or 0.0
    quote_amount = o.amount_quote or 0.0
    if o.side == "buy":
        holdings[quote] = holdings.get(quote, 0.0) + quote_amount
        holdings[base] = holdings.get(base, 0.0) - amount
    else:
        holdings[quote] = holdings.get(quote, 0.0) - quote_amount
        holdings[base] = holdings.get(base, 0.0) + amount
    for key in (base, quote):
        if abs(holdings.get(key, 0.0)) < 1e-12:
            holdings[key] = 0.0
