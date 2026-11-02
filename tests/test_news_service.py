import itertools
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.ai.budget import BudgetGuard
from app.ai.news_analyst import (
    Assessment,
    AssessmentBatch,
    NewsAnalyst,
    apply_assessments,
    normalize_key,
)
from app.db import Base, NewsItem, make_engine
from app.news.service import (
    NewsService,
    group_items,
    market_sentiment,
    pending_items,
    recent_event_keys,
    recent_items,
    requeue_items_without_event_key,
    store_items,
)
from app.news.sources import RawItem

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
_ids = itertools.count()


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
        url=url if "://" in url else f"https://news.test/{url}",
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
    host = f"{normalize_key(title) or 'x'}.test"
    it = NewsItem(
        source="rss",
        source_name="Src",
        # One outlet (feed) per title, and a unique URL per article.
        feed_host=host,
        url=f"https://{host}/{next(_ids)}",
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
    # A market-wide hack reported by two outlets (same event key).
    _item(session, "hack", -1.0, 3, "ALL", hours_ago=1).event_key = "exchange-hack"
    _item(session, "hack-2", -1.0, 3, "ALL", hours_ago=1).event_key = "exchange-hack"
    session.commit()
    _item(session, "etf", 1.0, 1, "BTC", hours_ago=1)
    _item(session, "eth-only", 1.0, 3, "ETH", hours_ago=1)  # one outlet: counts as impact 2
    _item(session, "noise", 1.0, 0, "BTC", hours_ago=1)  # impact 0 -> ignored
    _item(session, "irrelevant", 1.0, 3, "BTC", hours_ago=1, relevant=False)
    btc = market_sentiment(session, "BTC", 48, NOW)
    assert btc.items == 2 and btc.high_impact == 1 and btc.confirmed_high_impact == 1
    assert btc.score == pytest.approx((-3 + 1) / 4, abs=0.01)
    assert btc.label() == "negatief"
    assert btc.top[0].title == "hack"

    eth = market_sentiment(session, "ETH", 48, NOW)
    assert eth.items == 2 and eth.score == pytest.approx((-3 + 2) / 5, abs=0.01)

    # Old high-impact news fades: a 36h-old "days" item has half the weight.
    # The market-wide hack (ALL) counts for SOL too, with 1h of decay; the single-outlet SOL
    # items weigh as impact 2.
    _item(session, "old-good", 1.0, 3, "SOL", hours_ago=36)
    _item(session, "new-bad", -1.0, 3, "SOL", hours_ago=0)
    sol = market_sentiment(session, "SOL", 48, NOW)
    w_hack = 3 * math.exp(-math.log(2) * 1 / 36)
    assert sol.items == 3
    assert sol.score == pytest.approx((-2 + 1 - w_hack) / (3 + w_hack), abs=0.001)

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
                event_key="Binance Hack!",
            ),
            Assessment(
                id=ids["Cat video"],
                relevant=False,
                sentiment=0.5,
                impact=2,
                coins=["BTC"],
                event_key="cat-video",
            ),
        ]
    )
    analyst = make_analyst(batch)
    results = await analyst.assess(session, items, ["BTC", "ETH"], known_keys=["fed-rente"])
    assert len(results) == 2
    call = analyst._client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "Hack" in call["messages"][0]["content"]
    assert (
        "Bestaande sleutels (event_key) van de laatste 48 uur: fed-rente"
        in (call["messages"][0]["content"])
    )
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    assert apply_assessments(session, items, results, "claude-haiku-5-5") == 2
    hack = next(i for i in items if i.title == "Hack")
    cat = next(i for i in items if i.title == "Cat video")
    assert hack.assessed and hack.coins == "ALL,BTC" and hack.impact == 3
    assert hack.event_key == "binance-hack" and cat.event_key is None
    assert cat.assessed and not cat.relevant and cat.impact == 0 and cat.sentiment == 0.0
    assert pending_items(session, 10) == []


class FakeFetcher:
    def __init__(self, items):
        self.items = items

    async def fetch_all(self):
        return self.items


async def test_service_refresh_end_to_end(session):
    class EchoAnalyst(NewsAnalyst):
        async def assess(self, session, items, coins, known_keys=None):
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


def test_market_sentiment_counts_one_news_fact_once(session):
    # Six outlets report the same hack; one outlet reports good ETF news of equal impact.
    for i in range(6):
        it = _item(session, f"hack-{i}", -1.0, 3, "BTC", hours_ago=1)
        it.event_key = "binance-hack"
    _item(session, "etf", 1.0, 3, "BTC", hours_ago=1).event_key = "btc-etf-instroom"
    session.commit()
    btc = market_sentiment(session, "BTC", 48, NOW)
    assert btc.items == 2 and btc.articles == 7 and btc.high_impact == 2
    # One bad fact (six outlets, impact 3), one good fact (one outlet: impact at most 2).
    assert btc.score == pytest.approx((-3 + 2) / 5, abs=0.01)
    assert btc.top[0].sources == 6 and btc.top[1].sources == 1
    assert btc.as_dict()["top"][0]["sources"] == 6 and btc.as_dict()["articles"] == 7

    # The event takes the highest impact and the mean sentiment of its articles.
    _item(session, "mild", -0.2, 1, "SOL", hours_ago=1).event_key = "sol-outage"
    _item(session, "severe", -0.8, 3, "SOL", hours_ago=1).event_key = "sol-outage"
    session.commit()
    sol = market_sentiment(session, "SOL", 48, NOW)
    assert sol.items == 1 and sol.high_impact == 1 and sol.score == pytest.approx(-0.5)


def test_items_without_key_fall_back_to_title(session):
    _item(session, "Bitcoin ETF sees record inflows", 1.0, 2, "BTC", hours_ago=1)
    it = _item(session, "bitcoin etf sees record inflows!", 1.0, 2, "BTC", hours_ago=2)
    it.url = "https://y/other"
    session.commit()
    assert recent_event_keys(session, now=NOW) == []
    btc = market_sentiment(session, "BTC", 48, NOW)
    assert btc.items == 1 and btc.articles == 2


def test_recent_event_keys_ordered_by_frequency(session):
    for i in range(3):
        _item(session, f"a{i}", -1.0, 3, "BTC", hours_ago=1).event_key = "binance-hack"
    _item(session, "b", 1.0, 1, "BTC", hours_ago=1).event_key = "etf"
    _item(session, "old", 1.0, 1, "BTC", hours_ago=60).event_key = "oud-feit"
    session.commit()
    assert recent_event_keys(session, now=NOW) == ["binance-hack", "etf"]


def test_requeue_only_relevant_recent_items_without_key(session):
    keep = _item(session, "keyed", -1.0, 3, "BTC", hours_ago=1)
    keep.event_key = "x"
    _item(session, "nokey", -1.0, 3, "BTC", hours_ago=1)
    _item(session, "irrelevant", 0.0, 0, "", hours_ago=1, relevant=False)
    old = _item(session, "old", -1.0, 3, "BTC", hours_ago=100)
    old.published_at = datetime.now(UTC) - timedelta(hours=100)
    session.commit()
    # Fixed-clock items from _item are "now - 1h" relative to NOW; make them recent for real.
    for it in session.query(NewsItem).filter(NewsItem.title.in_(["keyed", "nokey", "irrelevant"])):
        it.published_at = datetime.now(UTC) - timedelta(hours=1)
    session.commit()
    assert requeue_items_without_event_key(session) == 1
    assert [it.title for it in pending_items(session, 10)] == ["nokey"]
    assert requeue_items_without_event_key(session) == 0


def test_group_items_folds_articles_per_news_fact(session):
    a = _item(session, "hack-a", -0.9, 3, "BTC", hours_ago=2)
    b = _item(session, "hack-b", -0.5, 2, "BTC,ALL", hours_ago=1)
    a.event_key = b.event_key = "binance-hack"
    b.source_name = "Other"
    solo = _item(session, "etf", 0.8, 1, "BTC", hours_ago=3)
    fresh = _item(session, "unassessed", None, None, None, hours_ago=0, relevant=None)
    fresh.assessed = False
    session.commit()
    groups = group_items([fresh, b, a, solo])
    assert [g.key for g in groups] == ["title:unassessed", "binance-hack", "title:etf"]
    hack = groups[1]
    assert hack.lead is a and hack.sources == 2 and hack.others == [b]
    assert hack.sentiment == pytest.approx(-0.7) and hack.impact == 3
    assert hack.coin_list() == ["BTC", "ALL"] and hack.relevant and hack.assessed
    assert not groups[0].assessed and groups[0].sentiment is None and groups[0].impact is None
    assert groups[2].sources == 1 and groups[2].others == []


def test_count_status_explains_why_an_article_does_or_does_not_count(session):
    from app.news.service import count_status

    tracked = ["BTC", "SOL"]
    ok = _item(session, "etf-inflow", 0.6, 2, "BTC", hours_ago=2)
    st = count_status(ok, tracked, now=NOW)
    assert st.counts and st.code == "counted" and st.coins == ("BTC",)
    assert st.reason_nl == "telt mee voor BTC"

    everything = _item(session, "macro", -0.2, 1, "ALL", hours_ago=1)
    assert count_status(everything, tracked, now=NOW).coins == ("BTC", "SOL")

    pending = _item(session, "fresh", None, None, None, hours_ago=0, relevant=None)
    pending.assessed = False
    assert count_status(pending, tracked, now=NOW).code == "pending"

    noise = _item(session, "celebrity", 0.0, 0, None, hours_ago=1, relevant=False)
    assert count_status(noise, tracked, now=NOW).code == "irrelevant"

    old = _item(session, "old-hack", -0.9, 3, "BTC", hours_ago=49)
    st = count_status(old, tracked, now=NOW)
    assert st.code == "too_old" and "48 uur" in st.reason_nl

    eth = _item(session, "eth-upgrade", 0.8, 3, "ETH,DOGE", hours_ago=1)
    st = count_status(eth, tracked, now=NOW)
    assert st.code == "other_coins" and "ETH, DOGE" in st.reason_nl

    zero = _item(session, "minor", 0.3, 0, "BTC", hours_ago=1)
    assert count_status(zero, tracked, now=NOW).code == "no_impact"


def test_annotate_groups_rolls_status_up_per_news_fact_and_filters(session):
    from app.news.service import annotate_groups

    tracked = ["BTC"]
    a = _item(session, "hack-a", -0.9, 3, "BTC", hours_ago=2)
    b = _item(session, "hack-b", None, None, None, hours_ago=1, relevant=None)
    b.assessed = False
    a.event_key = b.event_key = "binance-hack"
    eth = _item(session, "eth-upgrade", 0.8, 3, "ETH", hours_ago=1)
    session.commit()

    groups = annotate_groups(group_items([b, a, eth]), tracked, now=NOW)
    hack, upgrade = groups
    assert hack.status.counts and hack.status.reason_nl == "telt mee voor BTC"
    assert b.counting.code == "pending" and a.counting.counts
    assert not upgrade.status.counts and upgrade.status.code == "other_coins"

    only_counting = annotate_groups(group_items([b, a, eth]), tracked, counted=True, now=NOW)
    assert [g.key for g in only_counting] == ["binance-hack"]
    rest = annotate_groups(group_items([b, a, eth]), tracked, counted=False, now=NOW)
    assert [g.key for g in rest] == ["title:eth-upgrade"]

    # Two counting sources: the reason says the sentiment is an average of them.
    b.assessed, b.relevant, b.sentiment, b.impact, b.coins = True, True, -0.5, 2, "BTC"
    session.commit()
    (hack,) = annotate_groups(group_items([b, a]), tracked, now=NOW)
    assert "gemiddeld over 2 bronnen" in hack.status.reason_nl
