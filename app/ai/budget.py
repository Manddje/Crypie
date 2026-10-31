"""Track Claude spend per call and enforce a monthly budget.

Prices are USD per million tokens (Anthropic list prices, 2026-10). Unknown models fall
back to the most expensive known tier so an estimate never undershoots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import ClaudeUsage


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


class BudgetGuard:
    WARN_FRACTION = 0.9

    def __init__(self, monthly_limit_usd: float):
        self.limit = monthly_limit_usd

    def status(self, session: Session, now: datetime | None = None) -> dict[str, Any]:
        spent = month_spend(session, now)
        pct = (spent / self.limit * 100) if self.limit > 0 else 100.0
        return {
            "spent_usd": round(spent, 4),
            "limit_usd": self.limit,
            "pct": round(pct, 1),
            "warning": pct >= self.WARN_FRACTION * 100,
            "exhausted": spent >= self.limit,
            "month": month_start(now).strftime("%Y-%m"),
        }

    def ensure_available(self, session: Session, now: datetime | None = None) -> None:
        st = self.status(session, now)
        if st["exhausted"]:
            raise BudgetExceeded(
                f"Claude-budget van {self.limit:.2f} USD voor {st['month']} is op "
                f"({st['spent_usd']:.2f} USD gebruikt). Verhoog CLAUDE_MONTHLY_BUDGET_USD "
                "of wacht tot volgende maand."
            )
