import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.news.sources import NewsFetcher, parse_cryptopanic, parse_gdelt, parse_rss

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
