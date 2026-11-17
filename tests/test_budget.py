from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetExceeded, BudgetGuard, estimate_cost, month_spend, record_usage
from app.db import Base, make_engine


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def usage(inp=1000, out=500, cr=0, cw=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cw,
    )


def test_estimate_cost_known_and_unknown_model():
    assert estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0, cache_read_tokens=1_000_000) == 4.20
    # unknown model falls back to the most expensive tier
    assert estimate_cost("claude-unknown", 1_000_000, 0) == 10.0


def test_record_and_month_spend(session):
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    record_usage(session, "strategy", "claude-opus-5-5", usage(), now)
    record_usage(session, "news", "claude-haiku-5-5", usage(), now)
    # last month's spend must not count
    record_usage(
        session, "news", "claude-opus-5-5", usage(1_000_000, 0), datetime(2026, 9, 30, tzinfo=UTC)
    )
    spent = month_spend(session, now)
    assert spent == pytest.approx(0.014 + 0.00035)


def test_guard_warns_then_blocks(session):
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    guard = BudgetGuard(monthly_limit_usd=10.0)
    assert guard.status(session, now)["pct"] == 0
    guard.ensure_available(session, now)

    record_usage(session, "strategy", "claude-opus-5-5", usage(2_000_000, 50_000), now)  # 9.0 USD
    st = guard.status(session, now)
    assert st["warning"] and not st["exhausted"]
    guard.ensure_available(session, now)

    record_usage(session, "strategy", "claude-opus-5-5", usage(500_000, 0), now)  # +2.0 USD
    with pytest.raises(BudgetExceeded):
        guard.ensure_available(session, now)
