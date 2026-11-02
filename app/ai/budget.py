"""Track Claude spend per call and enforce a monthly budget.

Prices are USD per million tokens (Anthropic list prices, 2026-10). Unknown models fall
back to the most expensive known tier so an estimate never undershoots.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import ClaudeUsage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": Price(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": Price(10.00, 50.00, 0.25, 12.50),
}
FALLBACK_PRICE = PRICES["claude-fable-5-1"]


def price_for(model: str) -> Price:
    return PRICES.get(model, FALLBACK_PRICE)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    p = price_for(model)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000


def usage_from_response(usage: Any) -> dict[str, int]:
    """Pull token counts out of an SDK ``Usage`` object (attributes may be None)."""
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


class BudgetExceeded(RuntimeError):
    pass


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_spend(session: Session, now: datetime | None = None) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(ClaudeUsage.cost_usd), 0.0)).where(
            ClaudeUsage.created_at >= month_start(now)
        )
    )
    return float(total or 0.0)


def month_spend_by_purpose(session: Session, now: datetime | None = None) -> dict[str, float]:
    rows = session.execute(
        select(ClaudeUsage.purpose, func.coalesce(func.sum(ClaudeUsage.cost_usd), 0.0))
        .where(ClaudeUsage.created_at >= month_start(now))
        .group_by(ClaudeUsage.purpose)
    ).all()
    return {purpose: float(total or 0.0) for purpose, total in rows}


def record_usage(
    session: Session, purpose: str, model: str, usage: Any, now: datetime | None = None
) -> ClaudeUsage:
    tokens = usage_from_response(usage)
    row = ClaudeUsage(
        purpose=purpose,
        model=model,
        cost_usd=estimate_cost(model, **tokens),
        **tokens,
    )
    if now is not None:
        row.created_at = now
    session.add(row)
    session.commit()
    return row


# Most a purpose may spend of the monthly budget. News analysis runs on third-party input every
# few minutes; without a cap a flood of (crafted) articles could eat the budget the trading
# decisions need. Purposes not listed share whatever is left of the total.
PURPOSE_SHARES: dict[str, float] = {"news": 0.5}

# Rough characters per token for the input estimate; low on purpose so it never undershoots.
CHARS_PER_TOKEN = 3


@dataclass
class Reservation:
    """An estimated worst-case cost held against the budget while a call is in flight."""

    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    id: int = 0

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


class BudgetGuard:
    WARN_FRACTION = 0.9

    def __init__(self, monthly_limit_usd: float, shares: dict[str, float] | None = None):
        self.limit = monthly_limit_usd
        self.shares = dict(PURPOSE_SHARES if shares is None else shares)
        self._lock = threading.Lock()
        self._reserved: dict[int, Reservation] = {}
        self._next_id = 0

    def _limit(self) -> float:
        limit = float(self.limit)
        return limit if math.isfinite(limit) else 0.0

    def reserved(self, purpose: str | None = None) -> float:
        return sum(
            r.cost_usd for r in self._reserved.values() if purpose is None or r.purpose == purpose
        )

    def status(self, session: Session, now: datetime | None = None) -> dict[str, Any]:
        limit = self._limit()
        by_purpose = month_spend_by_purpose(session, now)
        spent = sum(by_purpose.values())
        pct = (spent / limit * 100) if limit > 0 else 100.0
        return {
            "spent_usd": round(spent, 4),
            "limit_usd": self.limit,
            "pct": round(pct, 1),
            "warning": pct >= self.WARN_FRACTION * 100,
            "exhausted": spent >= limit,
            "month": month_start(now).strftime("%Y-%m"),
            "reserved_usd": round(self.reserved(), 4),
            "by_purpose": {k: round(v, 4) for k, v in sorted(by_purpose.items())},
            "shares": {
                purpose: {
                    "share_pct": round(share * 100, 1),
                    "limit_usd": round(limit * share, 4),
                    "spent_usd": round(by_purpose.get(purpose, 0.0), 4),
                    "exhausted": by_purpose.get(purpose, 0.0) >= limit * share,
                }
                for purpose, share in self.shares.items()
            },
        }

    def ensure_available(self, session: Session, now: datetime | None = None) -> None:
        st = self.status(session, now)
        if st["exhausted"]:
            raise BudgetExceeded(self._exhausted_message(st))

    def _exhausted_message(self, st: dict[str, Any]) -> str:
        return (
            f"Claude-budget van {self.limit:.2f} USD voor {st['month']} is op "
            f"({st['spent_usd']:.2f} USD gebruikt). Verhoog CLAUDE_MONTHLY_BUDGET_USD "
            "of wacht tot volgende maand."
        )

    def reserve(
        self,
        session: Session,
        purpose: str,
        model: str,
        *,
        prompt_chars: int,
        max_tokens: int,
        now: datetime | None = None,
    ) -> Reservation:
        """Hold the worst-case cost of one call, or raise BudgetExceeded when it does not fit.

        The check and the reservation happen under one lock, so parallel calls cannot all pass
        the check before any of them is booked. Release with :meth:`release` after the call.
        """
        res = Reservation(
            purpose, model, prompt_chars // CHARS_PER_TOKEN + 1, max(int(max_tokens), 0)
        )
        with self._lock:
            st = self.status(session, now)
            limit = self._limit()
            if st["exhausted"]:
                raise BudgetExceeded(self._exhausted_message(st))
            if st["spent_usd"] + self.reserved() + res.cost_usd > limit:
                raise BudgetExceeded(
                    f"Claude-budget van {self.limit:.2f} USD voor {st['month']} is bijna op "
                    f"({st['spent_usd']:.2f} USD gebruikt); deze aanroep kan tot "
                    f"{res.cost_usd:.2f} USD kosten en past er niet meer in."
                )
            share = self.shares.get(purpose)
            if share is not None:
                cap = limit * share
                used = st["by_purpose"].get(purpose, 0.0) + self.reserved(purpose)
                if used + res.cost_usd > cap:
                    raise BudgetExceeded(
                        f"Het {purpose}-deel van het Claude-budget ({share * 100:.0f}% = "
                        f"{cap:.2f} USD voor {st['month']}) is op ({used:.2f} USD gebruikt). "
                        "De rest blijft over voor de handelsbeslissingen."
                    )
            self._next_id += 1
            res.id = self._next_id
            self._reserved[res.id] = res
        return res

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self._reserved.pop(reservation.id, None)


def _not_billed(exc: Exception) -> bool:
    """API errors that come back before the model ran are not billed by Anthropic."""
    if isinstance(exc, anthropic.APITimeoutError):
        return False  # the model may have run to the end before the read timed out
    return isinstance(exc, anthropic.APIStatusError | anthropic.APIConnectionError)


async def metered_call(
    budget: BudgetGuard,
    session: Session,
    purpose: str,
    model: str,
    *,
    prompt_chars: int,
    max_tokens: int,
    call: Callable[[], Awaitable[Any]],
) -> Any:
    """Reserve, make one Claude call, book the real usage, release the reservation.

    When the call fails after the model may have run (a response that does not parse, a cut-off
    answer, a timeout), the reserved estimate is booked instead: the SDK raises before we see
    the usage, but Anthropic still bills the tokens.
    """
    reservation = budget.reserve(
        session, purpose, model, prompt_chars=prompt_chars, max_tokens=max_tokens
    )
    try:
        try:
            response = await call()
        except Exception as exc:
            if not _not_billed(exc):
                try:
                    record_usage(
                        session,
                        purpose,
                        model,
                        SimpleNamespace(
                            input_tokens=reservation.input_tokens,
                            output_tokens=reservation.output_tokens,
                        ),
                    )
                except Exception:  # noqa: BLE001  # never hide the original error
                    session.rollback()
                    log.exception("could not book the cost of a failed Claude call")
            raise
        record_usage(session, purpose, model, response.usage)
        return response
    finally:
        budget.release(reservation)


USAGE_RETENTION_DAYS = 400  # the budget needs the current month; keep about a year of history


def prune_usage(session: Session, now: datetime | None = None) -> int:
    """Delete usage rows older than USAGE_RETENTION_DAYS. Returns the number deleted."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=USAGE_RETENTION_DAYS)
    stmt = delete(ClaudeUsage).where(ClaudeUsage.created_at < cutoff)
    result = session.execute(stmt.execution_options(synchronize_session=False))
    session.commit()
    return int(result.rowcount or 0)
