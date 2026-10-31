"""Hard risk limits. These run in code and are never delegated to Claude."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Decision, Position, Setting

PAUSE_KEY = "trading_paused"


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
    now = now or datetime.now(UTC)
    return int(
        session.scalar(
            select(func.count(Decision.id)).where(
                Decision.mode == mode,
                Decision.executed.is_(True),
                Decision.created_at >= _day_start(now),
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
) -> tuple[float, str | None]:
    """Return (allowed_amount_quote, block_reason). Amount may be reduced to fit limits."""
    now = now or datetime.now(UTC)
    if is_paused(session):
        return 0.0, "handelen is gepauzeerd (kill switch)"
    if trades_today(session, mode, now) >= limits.max_trades_per_day:
        return 0.0, f"maximum van {limits.max_trades_per_day} trades per dag bereikt"
    if portfolio_value > 0:
        loss_pct = -realized_pnl_today(session, mode, now) / portfolio_value * 100
        if loss_pct >= limits.daily_loss_limit_pct:
            return 0.0, f"dagverlieslimiet van {limits.daily_loss_limit_pct}% bereikt"
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
