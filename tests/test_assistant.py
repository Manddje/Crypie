from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetExceeded, BudgetGuard, month_spend
from app.ai.strategy_assistant import StrategyAssistant, StrategyProposal
from app.db import Base, make_engine
from app.strategy.model import DEFAULT_STRATEGY


class FakeMessages:
    def __init__(self, proposal, stop_reason="end_turn"):
        self.proposal = proposal
        self.stop_reason = stop_reason
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.proposal,
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(
                input_tokens=1200,
                output_tokens=300,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def make_assistant(fake, limit=100.0):
    a = StrategyAssistant("key", "claude-opus-5-5", BudgetGuard(limit))
    a._client = SimpleNamespace(messages=fake)
    return a


async def test_propose_returns_strategy_and_logs_usage(session):
    proposal = StrategyProposal(strategy=DEFAULT_STRATEGY, explanation="Test")
    fake = FakeMessages(proposal)
    a = make_assistant(fake)
    out = await a.propose(session, "koop laag, verkoop hoog", DEFAULT_STRATEGY, ["BTC-EUR"])
    assert out.strategy.name == DEFAULT_STRATEGY.name
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is StrategyProposal
    assert "koop laag" in call["messages"][0]["content"]
    assert month_spend(session) == pytest.approx(1200 * 4 / 1e6 + 300 * 20 / 1e6)


async def test_propose_rejects_market_outside_allowlist(session):
    a = make_assistant(FakeMessages(StrategyProposal(strategy=DEFAULT_STRATEGY, explanation="x")))
    with pytest.raises(ValueError, match="BTC-EUR"):
        await a.propose(session, "iets", DEFAULT_STRATEGY, ["ETH-EUR"])


async def test_propose_blocked_when_budget_exhausted(session):
    fake = FakeMessages(StrategyProposal(strategy=DEFAULT_STRATEGY, explanation="x"))
    a = make_assistant(fake, limit=0.0)
    with pytest.raises(BudgetExceeded):
        await a.propose(session, "iets", DEFAULT_STRATEGY, ["BTC-EUR"])
    assert fake.calls == []


async def test_propose_without_key_is_unavailable(session):
    a = StrategyAssistant("", "claude-opus-5-5", BudgetGuard(100))
    assert not a.available
    with pytest.raises(RuntimeError):
        await a.propose(session, "iets", DEFAULT_STRATEGY, ["BTC-EUR"])
