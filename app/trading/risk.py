"""Hard risk limits. These run in code and are never delegated to Claude."""

from __future__ import annotations

import math
from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import ACTIVE_POSITION_STATUSES, Order, Position, Setting

PAUSE_KEY = "trading_paused"
# Orders whose outcome is not known yet: no new order in that market until reconciled.
UNSETTLED_ORDER_STATUSES = ("pending", "unknown")


@dataclass(frozen=True)
class RiskLimits:
    max_trade_eur: float
    max_exposure_pct: float
    daily_loss_limit_pct: float
    max_trades_per_day: int
    cooldown_min_after_stop: int
    min_trade_eur: float


def is_paused(session: Session) -> bool:
    row = session.get(Setting, PAUSE_KEY)
    return bool(row and row.value == "1")


def set_paused(session: Session, paused: bool) -> None:
    row = session.get(Setting, PAUSE_KEY)
    if row is None:
        session.add(Setting(key=PAUSE_KEY, value="1" if paused else "0"))
    else:
        row.value = "1" if paused else "0"
    session.commit()


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def trades_today(session: Session, mode: str, now: datetime | None = None) -> int:
    """Orders of today that (may have) traded: every buy and sell that filled, from the bot,
    the exit loop or by hand, plus those whose outcome is not known yet (fail closed)."""
    now = now or datetime.now(UTC)
    return int(
        session.scalar(
            select(func.count(Order.id)).where(
                Order.mode == mode,
                Order.created_at >= _day_start(now),
                or_(
                    Order.status.in_(UNSETTLED_ORDER_STATUSES),
                    and_(Order.status != "rejected", Order.amount > 0),
                ),
            )
        )
        or 0
    )


def realized_pnl_today(session: Session, mode: str, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    return float(
        session.scalar(
            select(func.coalesce(func.sum(Position.pnl_eur), 0.0)).where(
                Position.mode == mode,
                Position.status == "closed",
                Position.closed_at >= _day_start(now),
            )
        )
        or 0.0
    )


def _active_positions(session: Session, mode: str) -> list[Position]:
    return list(
        session.scalars(
            select(Position).where(
                Position.mode == mode, Position.status.in_(ACTIVE_POSITION_STATUSES)
            )
        )
    )


def unrealized_pnl(session: Session, mode: str, prices: dict[str, float]) -> float:
    """Paper result of the open positions at ``prices`` (EUR, vs. what was spent incl. fee).

    A position whose market has no price in ``prices`` counts as break-even.
    """
    total = 0.0
    for p in _active_positions(session, mode):
        price = prices.get(p.market)
        if price and p.amount:
            total += p.amount * price - (p.entry_quote or 0.0)
    return total


def position_exposure(session: Session, mode: str, prices: dict[str, float]) -> float:
    """EUR value of the bot's own open positions (at entry value when there is no price)."""
    total = 0.0
    for p in _active_positions(session, mode):
        price = prices.get(p.market)
        total += p.amount * price if price else (p.entry_quote or 0.0)
    return total


def strategy_invested(
    session: Session, mode: str, markets: list[str], strategy_id: int | None = None
) -> float:
    """EUR spent (incl. fee) on the active positions in ``markets``, plus those the strategy
    opened itself in a market it no longer trades: what a strategy with a budget of its own
    has tied up right now. Buys in flight (``opening``) count too."""
    conditions = []
    if markets:
        conditions.append(Position.market.in_(list(markets)))
    if strategy_id is not None:
        conditions.append(Position.strategy_id == strategy_id)
    if not conditions:
        return 0.0
    return float(
        session.scalar(
            select(func.coalesce(func.sum(Position.entry_quote), 0.0)).where(
                Position.mode == mode,
                Position.status.in_(ACTIVE_POSITION_STATUSES),
                or_(*conditions),
            )
        )
        or 0.0
    )


def budget_room(
    session: Session,
    mode: str,
    markets: list[str],
    budget_eur: float,
    strategy_id: int | None = None,
) -> tuple[float, float]:
    """(invested, room) of a strategy's budget; room is unbounded (inf) without a budget."""
    invested = strategy_invested(session, mode, markets, strategy_id)
    if not budget_eur or budget_eur <= 0:
        return invested, math.inf
    return invested, max(budget_eur - invested, 0.0)


def has_unsettled_order(session: Session, mode: str, market: str) -> bool:
    """True while an order in ``market`` was sent but its outcome is not known yet."""
    return (
        session.scalar(
            select(func.count(Order.id)).where(
                Order.mode == mode,
                Order.market == market,
                Order.status.in_(UNSETTLED_ORDER_STATUSES),
            )
        )
        or 0
    ) > 0


def last_stop_loss(session: Session, mode: str, market: str) -> datetime | None:
    return session.scalar(
        select(func.max(Position.closed_at)).where(
            Position.mode == mode,
            Position.market == market,
            Position.close_reason == "stop_loss",
        )
    )


def check_buy(
    session: Session,
    limits: RiskLimits,
    mode: str,
    market: str,
    amount_quote: float,
    portfolio_value: float,
    exposure_value: float,
    now: datetime | None = None,
    unrealized: float = 0.0,
) -> tuple[float, str | None]:
    """Return (allowed_amount_quote, block_reason). Amount may be reduced to fit limits.

    ``unrealized`` is the paper result of the open positions at the current price; a loss in
    it counts towards the daily loss limit together with today's realized result. A paper
    gain does not: it may stem from days ago and must not hide today's realized losses.
    """
    now = now or datetime.now(UTC)
    if is_paused(session):
        return 0.0, "handelen is gepauzeerd (kill switch)"
    # Fail closed: NaN compares False, so it would slip through every check below.
    numbers = (*astuple(limits), amount_quote, portfolio_value, exposure_value, unrealized)
    if not all(math.isfinite(float(x)) for x in numbers):
        return 0.0, "ongeldige waarde in de limieten of de portefeuille (geen getal)"
    if trades_today(session, mode, now) >= limits.max_trades_per_day:
        return 0.0, f"maximum van {limits.max_trades_per_day} trades per dag bereikt"
    if portfolio_value > 0:
        day_result = realized_pnl_today(session, mode, now) + min(unrealized, 0.0)
        loss_pct = -day_result / portfolio_value * 100
        if loss_pct >= limits.daily_loss_limit_pct:
            return 0.0, (
                f"dagverlieslimiet van {limits.daily_loss_limit_pct}% bereikt "
                "(gerealiseerd plus ongerealiseerd)"
            )
    last_stop = last_stop_loss(session, mode, market)
    if last_stop is not None:
        if last_stop.tzinfo is None:
            last_stop = last_stop.replace(tzinfo=UTC)
        if now - last_stop < timedelta(minutes=limits.cooldown_min_after_stop):
            return 0.0, f"afkoelperiode na stop-loss in {market} loopt nog"
    amount = min(amount_quote, limits.max_trade_eur)
    room = portfolio_value * limits.max_exposure_pct / 100 - exposure_value
    amount = min(amount, max(room, 0.0))
    if amount < limits.min_trade_eur:
        if room < limits.min_trade_eur:
            return 0.0, f"blootstellingslimiet van {limits.max_exposure_pct}% bereikt"
        return 0.0, f"bedrag onder minimum van {limits.min_trade_eur} EUR"
    return round(amount, 2), None
