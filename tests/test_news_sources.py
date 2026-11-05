import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.news.sources import (
    NewsFetcher,
    coin_search_term,
    parse_cryptocv,
    parse_cryptopanic,
    parse_etf_flows_cryptoetf,
    parse_etf_flows_tftc,
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


# -- ETF flows (TFTC, cryptoetf.today) ---------------------------------------------------------


def _day(i: int, start=datetime(2026, 11, 1, tzinfo=UTC)) -> str:
    return (start + timedelta(days=i)).date().isoformat()


def _tftc_payload(flows_musd, funds=(("IBIT", 0.7), ("FBTC", 0.3), ("GBTC", 0.0))):
    """TFTC's data.json: one session per day (oldest first), flows in USD, per fund."""
    return {
        "name": "US Spot Bitcoin ETF Daily Flows",
        "days": [
            {
                "date": _day(i),
                "netFlowUsd": f * 1e6,
                "vendorNetFlowUsd": f * 1e6,
                "btcCloseUsd": 100_000 + i,
                "perEtfUsd": {t: f * 1e6 * w for t, w in funds},
            }
            for i, f in enumerate(flows_musd)
        ],
    }


def _cryptoetf_payload(flows_musd, symbol="ETH"):
    """cryptoetf.today's /flows/<asset>: 30 days of net flows in millions, oldest first."""
    return {
        "symbol": symbol,
        "windowDays": 30,
        "days": [{"date": _day(i), "netFlowUsdM": f} for i, f in enumerate(flows_musd)],
    }


def test_parse_tftc_flows_summarises_the_last_days():
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    payload = _tftc_payload([50, -20, 10, 100, 120, 80])  # 2026-11-01 .. 2026-11-06 (future)
    flows = parse_etf_flows_tftc(payload, now)
    assert flows["coin"] == "BTC" and flows["as_of"] == "2026-11-05"  # the future row is dropped
    assert flows["days"] == 5 and flows["last_day_musd"] == 120.0
    assert flows["sum_5d_musd"] == 260.0 and flows["sum_30d_musd"] == 260.0
    assert flows["typical_day_musd"] == 60.0  # (50+20+10+100+120)/5
    assert flows["streak_days"] == 3  # 10, 100, 120 after the -20
    assert flows["label"] == "strong_inflow"  # 260 / (5 * 60) = 0.87
    # Funds by size, zero flows left out.
    assert flows["last_day_funds"] == [
        {"ticker": "IBIT", "flow_musd": 84.0},
        {"ticker": "FBTC", "flow_musd": 36.0},
    ]
    assert flows["price_usd"] == 100_004.0 and flows["source"] == "tftc"
    assert [d["flow_musd"] for d in flows["daily"]] == [50.0, -20.0, 10.0, 100.0, 120.0]
    assert parse_etf_flows_tftc({"days": []}, now) is None
    assert parse_etf_flows_tftc({"days": [{"date": "x", "netFlowUsd": 1}]}, now) is None
    assert parse_etf_flows_tftc("nope", now) is None


def test_parse_cryptoetf_flows_labels_and_streaks():
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    out = parse_etf_flows_cryptoetf(
        _cryptoetf_payload([100, 100, 100, -10, -40, -50, -20, -10]), "eth", now
    )
    # The last 5 days: -130 against 5 x 53.75 typical = -0.48, five days of outflow in a row.
    assert out["coin"] == "ETH" and out["streak_days"] == -5 and out["label"] == "outflow"
    assert out["last_day_funds"] == [] and out["price_usd"] is None
    assert out["source"] == "cryptoetf.today"
    out = parse_etf_flows_cryptoetf(
        _cryptoetf_payload([100, 100, 100, -30, -40, -50, -20, -10]), "ETH", now
    )
    assert out["label"] == "strong_outflow"  # -150 / (5 x 56.25) = -0.53
    mixed = parse_etf_flows_cryptoetf(_cryptoetf_payload([100, -100, 100, -100, 10]), "ETH", now)
    assert mixed["label"] == "mixed" and mixed["streak_days"] == 1
    assert (
        parse_etf_flows_cryptoetf(_cryptoetf_payload([0, 0, 0]), "ETH", now)["label"] == "unknown"
    )
    # The same day twice: the later row wins; a day without a usable flow is skipped.
    payload = {
        "days": [
            {"date": "2026-11-05", "netFlowUsdM": "-250"},
            {"date": "2026-11-05T00:00:00Z", "netFlowUsdM": -300},
            {"date": "2026-11-04", "netFlowUsdM": "nan"},
        ]
    }
    out = parse_etf_flows_cryptoetf(payload, "SOL", now)
    assert out["as_of"] == "2026-11-05" and out["last_day_musd"] == -300.0 and out["days"] == 1
    assert parse_etf_flows_cryptoetf({"days": "x"}, "ETH", now) is None


@respx.mock
async def test_fetcher_etf_flows_with_a_key_asks_cryptoetf_per_coin():
    btc = respx.get(NewsFetcher.CRYPTOETF_URL.format(asset="btc")).mock(
        return_value=Response(200, json=_cryptoetf_payload([10, 20, 30], "BTC"))
    )
    eth = respx.get(NewsFetcher.CRYPTOETF_URL.format(asset="eth")).mock(
        return_value=Response(401, json={"message": "API key required", "statusCode": 401})
    )
    tftc = respx.get(NewsFetcher.TFTC_URL).mock(return_value=Response(500))
    f = NewsFetcher([], None, coins=["BTC", "ETH", "PEPE"], cryptoetf_api_key="cetf_live_x")
    try:
        flows = await f.fetch_etf_flows()
        assert set(flows) == {"BTC", "ETH"}  # PEPE has no ETF
        assert flows["BTC"]["coin"] == "BTC" and flows["BTC"]["days"] == 3
        assert flows["ETH"] is None  # a refused key counts as a failure, nothing else breaks
        assert btc.calls.last.request.headers["Authorization"] == "Bearer cetf_live_x"
        assert eth.call_count == 1 and tftc.call_count == 0
        # The figures change once a trading day: a second call within the hour is answered
        # from memory.
        assert await f.fetch_etf_flows() is flows
        assert btc.call_count == 1
    finally:
        await f.aclose()


@respx.mock
async def test_fetcher_etf_flows_without_a_key_reads_tftc_for_bitcoin_only():
    tftc = respx.get(NewsFetcher.TFTC_URL).mock(
        return_value=Response(200, json=_tftc_payload([10, 20, 30]))
    )
    cetf = respx.get(url__startswith="https://api.cryptoetf.today/").mock(
        return_value=Response(200, json=_cryptoetf_payload([1]))
    )
    f = NewsFetcher([], None, coins=["ETH", "BTC"])
    try:
        assert f.etf_flow_coins() == ["BTC"]
        flows = await f.fetch_etf_flows()
        assert set(flows) == {"BTC"} and flows["BTC"]["source"] == "tftc"
        assert "Authorization" not in tftc.calls.last.request.headers
        assert cetf.call_count == 0
    finally:
        await f.aclose()

    for off in (
        NewsFetcher([], None, coins=["ETH"]),  # no key and no bitcoin: nothing to fetch
        NewsFetcher([], None, coins=["BTC"], cryptoetf_api_key="k", etf_flows_enabled=False),
    ):
        try:
            assert off.etf_flow_coins() == [] and await off.fetch_etf_flows() == {}
        finally:
            await off.aclose()
    assert tftc.call_count == 1 and cetf.call_count == 0
