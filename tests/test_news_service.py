import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetGuard
from app.ai.news_analyst import Assessment, AssessmentBatch, NewsAnalyst, apply_assessments
from app.db import Base, NewsItem, make_engine
from app.news.service import (
    NewsService,
    market_sentiment,
    pending_items,
    recent_items,
    store_items,
)
from app.news.sources import RawItem

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def raw(url, title="t", hours_ago=1.0, coins=None, source="rss"):
    return RawItem(
        source=source,
        source_name="Src",
        url=url,
        title=title,
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        coins=coins or [],
    )


def test_store_items_dedupes_and_drops_old(session):
    n = store_items(session, [raw("u1"), raw("u1"), raw("u2"), raw("u3", hours_ago=100)], 48)
    assert n == 2
    assert store_items(session, [raw("u1"), raw("u4")], 48) == 1
    assert len(pending_items(session, 10)) == 3
    assert len(recent_items(session, 24)) == 3


def _item(session, title, sentiment, impact, coins, hours_ago, horizon="days", relevant=True):
    it = NewsItem(
        source="rss",
        source_name="Src",
        url=f"https://x/{title}",
        title=title,
        published_at=NOW - timedelta(hours=hours_ago),
        assessed=True,
        relevant=relevant,
        sentiment=sentiment,
        impact=impact,
        horizon=horizon,
        coins=coins,
    )
    session.add(it)
    session.commit()
    return it


def test_market_sentiment_weights_impact_and_recency(session):
    assert market_sentiment(session, "XRP", 48, NOW).label() == "geen nieuws"
    _item(session, "hack", -1.0, 3, "ALL", hours_ago=1)
    _item(session, "etf", 1.0, 1, "BTC", hours_ago=1)
    _item(session, "eth-only", 1.0, 3, "ETH", hours_ago=1)
    _item(session, "noise", 1.0, 0, "BTC", hours_ago=1)  # impact 0 -> ignored
    _item(session, "irrelevant", 1.0, 3, "BTC", hours_ago=1, relevant=False)
    btc = market_sentiment(session, "BTC", 48, NOW)
    assert btc.items == 2 and btc.high_impact == 1
    assert btc.score == pytest.approx((-3 + 1) / 4, abs=0.01)
    assert btc.label() == "negatief"
    assert btc.top[0].title == "hack"

    eth = market_sentiment(session, "ETH", 48, NOW)
    assert eth.items == 2 and eth.score == pytest.approx(0.0, abs=0.01)

    # Old high-impact news fades: a 36h-old "days" item has half the weight.
    # The market-wide "hack" item (ALL) counts for SOL too, with 1h of decay.
    _item(session, "old-good", 1.0, 3, "SOL", hours_ago=36)
    _item(session, "new-bad", -1.0, 3, "SOL", hours_ago=0)
    sol = market_sentiment(session, "SOL", 48, NOW)
    w_hack = 3 * math.exp(-math.log(2) * 1 / 36)
    assert sol.items == 3
    assert sol.score == pytest.approx((-3 + 1.5 - w_hack) / (4.5 + w_hack), abs=0.001)

    # Market-wide news reaches coins without their own items too.
    assert market_sentiment(session, "XRP", 48, NOW).label() == "negatief"


class FakeMessages:
    def __init__(self, batch):
        self.batch = batch
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.batch,
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=3000,
                output_tokens=400,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


def make_analyst(batch, limit=100.0):
    a = NewsAnalyst("key", "claude-haiku-5-5", BudgetGuard(limit))
    a._client = SimpleNamespace(messages=FakeMessages(batch))
    return a


async def test_analyst_assess_and_apply(session):
    store_items(session, [raw("a", "Hack"), raw("b", "Cat video")], 48)
    items = pending_items(session, 10)
    ids = {it.title: it.id for it in items}
    batch = AssessmentBatch(
        assessments=[
            Assessment(
                id=ids["Hack"],
                relevant=True,
                sentiment=-0.9,
                impact=3,
                horizon="days",
                coins=["btc", "ALL"],
                summary_nl="Grote hack.",
            ),
            Assessment(id=ids["Cat video"], relevant=False, sentiment=0.5, impact=2, coins=["BTC"]),
        ]
    )
    analyst = make_analyst(batch)
    results = await analyst.assess(session, items, ["BTC", "ETH"])
    assert len(results) == 2
    call = analyst._client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "Hack" in call["messages"][0]["content"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    assert apply_assessments(session, items, results, "claude-haiku-5-5") == 2
    hack = next(i for i in items if i.title == "Hack")
    cat = next(i for i in items if i.title == "Cat video")
    assert hack.assessed and hack.coins == "ALL,BTC" and hack.impact == 3
    assert cat.assessed and not cat.relevant and cat.impact == 0 and cat.sentiment == 0.0
    assert pending_items(session, 10) == []


class FakeFetcher:
    def __init__(self, items):
        self.items = items

    async def fetch_all(self):
        return self.items


async def test_service_refresh_end_to_end(session):
    class EchoAnalyst(NewsAnalyst):
        async def assess(self, session, items, coins):
            return [
                Assessment(id=it.id, relevant=True, sentiment=0.5, impact=1, coins=["BTC"])
                for it in items
            ]

    analyst = EchoAnalyst("key", "claude-haiku-5-5", BudgetGuard(100))
    service = NewsService(FakeFetcher([raw("a"), raw("b"), raw("c")]), analyst, ["BTC"], 48, 2)
    result = await service.refresh(session)
    assert result["inserted"] == 3 and result["assessed"] == 3 and result["pending"] == 0
    assert result["error"] is None and service.last_run is not None

    # Second run: nothing new, nothing pending.
    result = await service.refresh(session)
    assert result["inserted"] == 0 and result["assessed"] == 0


async def test_service_without_key_only_fetches(session):
    analyst = NewsAnalyst("", "claude-haiku-5-5", BudgetGuard(100))
    service = NewsService(FakeFetcher([raw("a")]), analyst, ["BTC"], 48, 10)
    result = await service.refresh(session)
    assert result["inserted"] == 1 and result["assessed"] == 0 and result["pending"] == 1
    assert "ANTHROPIC_API_KEY" in result["error"]


async def test_service_stops_when_budget_exhausted(session):
    batch = AssessmentBatch(assessments=[])
    analyst = make_analyst(batch, limit=0.0)
    service = NewsService(FakeFetcher([raw("a")]), analyst, ["BTC"], 48, 10)
    result = await service.refresh(session)
    assert result["assessed"] == 0 and "budget" in result["error"].lower()
    assert analyst._client.messages.calls == []
