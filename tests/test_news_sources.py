import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.news.sources import (
    NewsFetcher,
    coin_search_term,
    parse_cryptocv,
    parse_cryptopanic,
    parse_fear_greed,
    parse_gdelt,
    parse_google_news,
    parse_rss,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_rss_strips_html_and_skips_broken_entries():
    items = parse_rss((FIX / "feed.xml").read_bytes(), "https://feed")
    assert [i.url for i in items] == ["https://example.com/a", "https://example.com/old"]
    first = items[0]
    assert first.source == "rss" and first.source_name == "CoinTest"
    assert first.title == "Binance & $6.3B hack sparks debate"
    assert first.summary == "Largest hack on record."
    assert first.published_at == datetime(2026, 10, 31, 9, 58, 50, tzinfo=UTC)


def test_parse_gdelt_handles_bad_dates():
    items = parse_gdelt(json.loads((FIX / "gdelt.json").read_text()))
    assert len(items) == 2
    assert items[0].published_at == datetime(2026, 10, 31, 10, 15, tzinfo=UTC)
    assert items[0].source_name == "news.example.org"
    assert items[1].published_at.tzinfo is not None


def test_parse_cryptopanic_uses_original_url_and_instruments():
    items = parse_cryptopanic(json.loads((FIX / "cryptopanic.json").read_text()))
    (it,) = items
    assert it.url == "https://coindesk.com/etf"
    assert it.coins == ["BTC"] and it.votes["positive"] == 10
    assert it.source_name == "CoinDesk"


@respx.mock
async def test_fetcher_survives_broken_feed_and_rate_limit():
    respx.get("https://feed.ok/rss").mock(
        return_value=Response(200, content=(FIX / "feed.xml").read_bytes())
    )
    respx.get("https://feed.broken/rss").mock(return_value=Response(500))
    respx.get(NewsFetcher.GDELT_URL).mock(return_value=Response(429, text="slow down"))
    cp = respx.get("https://cryptopanic.com/api/growth/v2/posts/").mock(
        return_value=Response(200, json=json.loads((FIX / "cryptopanic.json").read_text()))
    )
    f = NewsFetcher(
        ["https://feed.ok/rss", "https://feed.broken/rss"],
        "bitcoin",
        cryptopanic_token="tok",
        coins=["BTC", "ETH"],
    )
    try:
        items = await f.fetch_all()
    finally:
        await f.aclose()
    assert sorted(i.source for i in items) == ["cryptopanic", "rss", "rss"]
    assert cp.calls.last.request.url.params["currencies"] == "BTC,ETH"


async def test_fetcher_skips_disabled_sources():
    f = NewsFetcher([], None, cryptopanic_token="")
    try:
        assert await f.fetch_gdelt() == []
        assert await f.fetch_cryptopanic() == []
    finally:
        await f.aclose()


@pytest.mark.parametrize("fn", [parse_gdelt, parse_cryptopanic])
def test_parsers_tolerate_empty_payload(fn):
    assert fn({}) == []


def test_parse_cryptocv_tags_coins_and_keeps_its_sentiment():
    payload = {
        "articles": [
            {
                "title": "Solana ETF inflows hit record",
                "link": "https://x.test/a?utm=1",
                "canonical_link": "https://x.test/a",
                "description": "<p>Inflows</p> rose",
                "source": "The Block",
                "pub_date": "2026-10-31T12:00:00.000Z",
                "tickers": ["SOL", "usdt"],
                "sentiment": {"score": 0.6, "label": "positive", "confidence": 0.8},
            },
            {"title": "no link"},
            {"title": "General item", "link": "https://x.test/b", "pubDate": "bad"},
        ]
    }
    items = parse_cryptocv(payload, "SOL")
    assert len(items) == 2
    assert items[0].source == "cryptocv" and items[0].url == "https://x.test/a"
    assert items[0].coins == ["SOL", "USDT"] and items[0].votes["score"] == 0.6
    assert items[0].summary == "Inflows rose" and items[0].source_name == "The Block"
    assert items[1].coins == ["SOL"]  # no tickers: the coin we asked for
    assert parse_cryptocv(payload)[1].coins == []


def test_parse_google_news_splits_publisher_and_tags_coin():
    content = (FIX / "feed.xml").read_bytes()
    items = parse_google_news(content, "https://news.google.com/rss/search?q=solana", "sol")
    assert items and all(i.source == "googlenews" and i.coins == ["SOL"] for i in items)
    assert coin_search_term("BTC") == "bitcoin" and coin_search_term("PEPE") == "PEPE crypto"


def test_parse_fear_greed():
    payload = {"data": [{"value": "30", "value_classification": "Fear", "timestamp": "1793404800"}]}
    mood = parse_fear_greed(payload)
    assert mood["value"] == 30 and mood["label"] == "Fear" and mood["at"].startswith("2026-10-31")
    assert parse_fear_greed({}) is None and parse_fear_greed({"data": [{"value": "x"}]}) is None


@respx.mock
async def test_fetcher_free_sources_per_coin():
    archive = respx.get(NewsFetcher.CRYPTOCV_ARCHIVE_URL).mock(
        return_value=Response(
            200,
            json={
                "articles": [
                    {
                        "title": "t",
                        "link": "https://a.test/1",
                        "pub_date": "2026-10-31T12:00:00Z",
                        "tickers": ["SOL"],
                    }
                ]
            },
        )
    )
    respx.get(NewsFetcher.CRYPTOCV_NEWS_URL).mock(return_value=Response(500))
    gnews = respx.get(NewsFetcher.GOOGLE_NEWS_URL).mock(
        return_value=Response(200, content=(FIX / "feed.xml").read_bytes())
    )
    respx.get(NewsFetcher.FEAR_GREED_URL).mock(
        return_value=Response(
            200, json={"data": [{"value": "72", "value_classification": "Greed"}]}
        )
    )
    f = NewsFetcher([], None, coins=["SOL"])
    try:
        items = await f.fetch_all()
        mood = await f.fetch_fear_greed()
    finally:
        await f.aclose()
    assert archive.calls.last.request.url.params["ticker"] == "SOL"
    assert "solana when:1d" in gnews.calls.last.request.url.params["q"]
    assert {i.source for i in items} == {"cryptocv", "googlenews"}
    assert mood == {"value": 72, "label": "Greed", "at": None}

    off = NewsFetcher([], None, coins=["SOL"], cryptocv_enabled=False, google_news_enabled=False)
    try:
        assert await off.fetch_cryptocv() == [] and await off.fetch_google_news() == []
    finally:
        await off.aclose()
