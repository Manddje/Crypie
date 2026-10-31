"""Fetch raw news from RSS feeds, GDELT, cryptocurrency.cv, Google News and CryptoPanic."""

from __future__ import annotations

import logging
from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Crypie/0.1 (+https://github.com/Manddje/Crypie)"


@dataclass
class RawItem:
    source: str
    source_name: str
    url: str
    title: str
    published_at: datetime
    summary: str | None = None
    coins: list[str] = field(default_factory=list)
    votes: dict[str, Any] | None = None


def _clean(text: str | None, limit: int = 2000) -> str | None:
    if not text:
        return None
    # feedparser leaves HTML in summaries; strip tags cheaply.
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or None


def parse_rss(content: bytes, feed_url: str, now: datetime | None = None) -> list[RawItem]:
    parsed = feedparser.parse(content)
    feed_title = (parsed.feed.get("title") or feed_url).strip()
    now = now or datetime.now(UTC)
    items: list[RawItem] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(timegm(stamp), UTC) if stamp else now
        items.append(
            RawItem(
                source="rss",
                source_name=feed_title[:128],
                url=link.strip()[:1024],
                title=_clean(title, 500) or title[:500],
                published_at=published,
                summary=_clean(entry.get("summary")),
            )
        )
    return items


def parse_gdelt(payload: dict[str, Any]) -> list[RawItem]:
    items: list[RawItem] = []
    for art in payload.get("articles", []):
        url = art.get("url")
        title = art.get("title")
        if not url or not title:
            continue
        seen = art.get("seendate")  # e.g. 20261031T101500Z
        try:
            published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (TypeError, ValueError):
            published = datetime.now(UTC)
        items.append(
            RawItem(
                source="gdelt",
                source_name=(art.get("domain") or "gdelt")[:128],
                url=url.strip()[:1024],
                title=_clean(title, 500) or title[:500],
                published_at=published,
            )
        )
    return items


def parse_cryptopanic(payload: dict[str, Any]) -> list[RawItem]:
    items: list[RawItem] = []
    for post in payload.get("results", []):
        url = post.get("original_url") or post.get("url")
        title = post.get("title")
        if not url or not title:
            continue
        try:
            published = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            published = datetime.now(UTC)
        instruments = post.get("instruments") or post.get("currencies") or []
        coins = [str(i.get("code", "")).upper() for i in instruments if i.get("code")]
        source = post.get("source") or {}
        items.append(
            RawItem(
                source="cryptopanic",
                source_name=(source.get("title") or "CryptoPanic")[:128],
                url=url.strip()[:1024],
                title=_clean(title, 500) or title[:500],
                published_at=published,
                summary=_clean(post.get("description")),
                coins=coins,
                votes=post.get("votes"),
            )
        )
    return items


def _iso(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(UTC)


def parse_cryptocv(payload: dict[str, Any], coin: str | None = None) -> list[RawItem]:
    """cryptocurrency.cv ``/api/archive?ticker=X`` and ``/api/news`` share one article shape."""
    items: list[RawItem] = []
    for art in payload.get("articles", []):
        url = art.get("canonical_link") or art.get("link")
        title = art.get("title")
        if not url or not title:
            continue
        tickers = [str(t).upper() for t in (art.get("tickers") or []) if t]
        coins = tickers or ([coin] if coin else [])
        sentiment = art.get("sentiment")
        items.append(
            RawItem(
                source="cryptocv",
                source_name=(art.get("source") or "cryptocurrency.cv")[:128],
                url=url.strip()[:1024],
                title=_clean(title, 500) or title[:500],
                published_at=_iso(art.get("pub_date") or art.get("pubDate")),
                summary=_clean(art.get("description")),
                coins=coins,
                votes=sentiment if isinstance(sentiment, dict) else None,
            )
        )
    return items


# Search terms for Google News per coin; anything else searches "<COIN> crypto".
COIN_SEARCH_TERMS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple xrp",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "BNB": "binance coin bnb",
    "AVAX": "avalanche avax crypto",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "LTC": "litecoin",
    "MATIC": "polygon matic",
    "POL": "polygon pol",
}


def coin_search_term(coin: str) -> str:
    return COIN_SEARCH_TERMS.get(coin.upper(), f"{coin} crypto")


def parse_google_news(content: bytes, feed_url: str, coin: str) -> list[RawItem]:
    """Google News RSS: titles end in " - Publisher"; items are about the searched coin."""
    items = parse_rss(content, feed_url)
    for it in items:
        it.source = "googlenews"
        it.coins = [coin.upper()]
        head, sep, publisher = it.title.rpartition(" - ")
        if sep and head and len(publisher) <= 60:
            it.title, it.source_name = head, publisher[:128]
        else:
            it.source_name = "Google News"
    return items


def parse_fear_greed(payload: dict[str, Any]) -> dict[str, Any] | None:
    """alternative.me Fear & Greed: {"value": 30, "label": "Fear", "at": "..."} or None."""
    data = payload.get("data") or []
    if not data:
        return None
    first = data[0]
    try:
        value = int(first["value"])
    except (KeyError, TypeError, ValueError):
        return None
    at = first.get("timestamp")
    try:
        when = datetime.fromtimestamp(int(at), UTC).isoformat() if at else None
    except (TypeError, ValueError):
        when = None
    return {"value": value, "label": first.get("value_classification") or "", "at": when}


class NewsFetcher:
    GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
    CRYPTOPANIC_URL = "https://cryptopanic.com/api/{plan}/v2/posts/"
    CRYPTOCV_ARCHIVE_URL = "https://cryptocurrency.cv/api/archive"
    CRYPTOCV_NEWS_URL = "https://cryptocurrency.cv/api/news"
    GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
    FEAR_GREED_URL = "https://api.alternative.me/fng/"

    def __init__(
        self,
        rss_feeds: list[str],
        gdelt_query: str | None,
        cryptopanic_token: str = "",
        cryptopanic_plan: str = "growth",
        coins: list[str] | None = None,
        timeout: float = 20.0,
        cryptocv_enabled: bool = True,
        google_news_enabled: bool = True,
        fear_greed_enabled: bool = True,
    ):
        self.rss_feeds = rss_feeds
        self.gdelt_query = gdelt_query
        self.cryptopanic_token = cryptopanic_token
        self.cryptopanic_plan = cryptopanic_plan
        self.coins = coins or []
        self.cryptocv_enabled = cryptocv_enabled
        self.google_news_enabled = google_news_enabled
        self.fear_greed_enabled = fear_greed_enabled
        self.fear_greed: dict[str, Any] | None = None
        self._http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_rss(self) -> list[RawItem]:
        items: list[RawItem] = []
        for url in self.rss_feeds:
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
                items.extend(parse_rss(resp.content, url))
            except Exception as exc:  # one broken feed must not stop the rest
                log.warning("RSS feed %s failed: %s", url, exc)
        return items

    async def fetch_gdelt(self, timespan: str = "1h", maxrecords: int = 75) -> list[RawItem]:
        if not self.gdelt_query:
            return []
        try:
            resp = await self._http.get(
                self.GDELT_URL,
                params={
                    "query": self.gdelt_query,
                    "mode": "ArtList",
                    "format": "json",
                    "timespan": timespan,
                    "maxrecords": maxrecords,
                    "sort": "DateDesc",
                },
            )
            if resp.status_code == 429:
                log.warning("GDELT rate limit hit; skipping this cycle")
                return []
            resp.raise_for_status()
            return parse_gdelt(resp.json())
        except Exception as exc:
            log.warning("GDELT fetch failed: %s", exc)
            return []

    async def fetch_cryptopanic(self) -> list[RawItem]:
        if not self.cryptopanic_token:
            return []
        params: dict[str, str] = {"auth_token": self.cryptopanic_token, "public": "true"}
        if self.coins:
            params["currencies"] = ",".join(self.coins)
        try:
            resp = await self._http.get(
                self.CRYPTOPANIC_URL.format(plan=self.cryptopanic_plan), params=params
            )
            resp.raise_for_status()
            return parse_cryptopanic(resp.json())
        except Exception as exc:
            log.warning("CryptoPanic fetch failed: %s", exc)
            return []

    async def fetch_cryptocv(self, per_coin: int = 40, general: int = 100) -> list[RawItem]:
        """Free aggregator (300+ sources): the archive per coin plus the general feed."""
        if not self.cryptocv_enabled:
            return []
        items: list[RawItem] = []
        for coin in self.coins:
            try:
                resp = await self._http.get(
                    self.CRYPTOCV_ARCHIVE_URL, params={"ticker": coin, "limit": per_coin}
                )
                resp.raise_for_status()
                items.extend(parse_cryptocv(resp.json(), coin))
            except Exception as exc:  # noqa: BLE001
                log.warning("cryptocurrency.cv archive for %s failed: %s", coin, exc)
        try:
            resp = await self._http.get(self.CRYPTOCV_NEWS_URL, params={"limit": general})
            resp.raise_for_status()
            items.extend(parse_cryptocv(resp.json()))
        except Exception as exc:  # noqa: BLE001
            log.warning("cryptocurrency.cv news failed: %s", exc)
        return items

    async def fetch_google_news(self, window: str = "1d") -> list[RawItem]:
        """One Google News search feed per coin, last day."""
        if not self.google_news_enabled:
            return []
        items: list[RawItem] = []
        for coin in self.coins:
            params = {
                "q": f"{coin_search_term(coin)} when:{window}",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            }
            try:
                resp = await self._http.get(self.GOOGLE_NEWS_URL, params=params)
                resp.raise_for_status()
                items.extend(parse_google_news(resp.content, str(resp.url), coin))
            except Exception as exc:  # noqa: BLE001
                log.warning("Google News for %s failed: %s", coin, exc)
        return items

    async def fetch_fear_greed(self) -> dict[str, Any] | None:
        """Market-wide Fear & Greed index (0 = extreme fear, 100 = extreme greed)."""
        if not self.fear_greed_enabled:
            return None
        try:
            resp = await self._http.get(self.FEAR_GREED_URL, params={"limit": 1})
            resp.raise_for_status()
            self.fear_greed = parse_fear_greed(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.warning("Fear & Greed fetch failed: %s", exc)
        return self.fear_greed

    async def fetch_all(self) -> list[RawItem]:
        items = await self.fetch_rss()
        items += await self.fetch_gdelt()
        items += await self.fetch_cryptocv()
        items += await self.fetch_google_news()
        items += await self.fetch_cryptopanic()
        return items
