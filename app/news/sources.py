"""Fetch raw news from RSS feeds, GDELT, cryptocurrency.cv, Google News and CryptoPanic.

Everything here is untrusted third-party input. Every fetch goes through :meth:`NewsFetcher._get`
(public hosts only, redirects checked per hop, a byte cap and a deadline), and every parser
checks types per record, so one bad article or one bad feed is skipped, never fatal.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import re
import socket
from calendar import timegm
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Crypie/0.1 (+https://github.com/Manddje/Crypie)"

MAX_BODY_BYTES = 5_000_000  # one response; a feed is a few hundred KB
REQUEST_DEADLINE_S = 30.0  # one request: every redirect hop plus the whole body
SOURCE_DEADLINE_S = 120.0  # one source with all its requests
MAX_REDIRECTS = 3
MAX_ITEMS_PER_RESPONSE = 200
MAX_COINS = 10  # 10 x (ticker + comma) fits source_coins VARCHAR(128)
MAX_VOTES_JSON = 2000
URL_MAX = 1024


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


# -- validation helpers -------------------------------------------------------------------

# Linear: a tag cannot contain "<", so a string full of "<" without ">" is scanned once.
_TAG_RE = re.compile(r"<[^<>]*>")
_WS_RE = re.compile(r"\s+")
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,10}$")


def _clean(text: Any, limit: int = 2000) -> str | None:
    if not isinstance(text, str) or not text:
        return None
    # feedparser leaves HTML in summaries; strip tags cheaply. Cut first: the result is at most
    # ``limit`` long anyway, and a huge input should not cost time.
    text = text[: limit * 4].replace("\x00", "")
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit] or None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def safe_url(value: Any) -> str | None:
    """``value`` when it is an absolute http(s) URL with a host and fits the column, else None."""
    s = _str(value)
    if s is None:
        return None
    s = s.strip()
    if len(s) > URL_MAX or any(ord(ch) < 33 or ch == "\x7f" for ch in s):
        return None
    try:
        parts = urlsplit(s)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or not parts.hostname:
        return None
    return s


def clean_tickers(raw: Any, limit: int = MAX_COINS) -> list[str]:
    """Upper-case tickers of 2-10 letters/digits, de-duplicated, at most ``limit``."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list | tuple):
        return []
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str | int):
            continue
        t = str(t).strip().upper()
        if _TICKER_RE.match(t) and t not in out:
            out.append(t)
    return out[:limit]


def aware(dt: datetime) -> datetime:
    """A naive datetime is taken as UTC; an aware one is converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _iso(value: Any) -> datetime | None:
    """ISO-8601 (also a bare date) as an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError):
        return None


def _records(payload: Any, key: str) -> list[dict[str, Any]]:
    """The dict records under ``payload[key]``, at most MAX_ITEMS_PER_RESPONSE."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [r for r in rows[:MAX_ITEMS_PER_RESPONSE] if isinstance(r, dict)]


def _title(value: Any) -> str | None:
    s = _str(value)
    return (_clean(s, 500) or s[:500].strip() or None) if s else None


# -- SSRF guard ---------------------------------------------------------------------------


class UnsafeURLError(ValueError):
    """A URL we will not fetch: not http(s), credentials, an IP literal or a non-public host."""


_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")


def validate_feed_url(url: Any) -> str:
    """Check a news/feed URL before it is stored or fetched; returns the stripped URL.

    Only http(s) with a dotted host name; no user:password@, no IP literal (also not in decimal
    or hex form) and no local names. Raises ValueError with a Dutch message. Whether the name
    resolves to a public address is checked at fetch time (see :func:`check_public_url`).
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("lege URL")
    u = url.strip()
    if len(u) > URL_MAX:
        raise UnsafeURLError(f"URL is langer dan {URL_MAX} tekens")
    if any(ord(ch) < 33 or ch == "\x7f" for ch in u):
        raise UnsafeURLError("URL bevat spaties of stuurtekens")
    try:
        parts = urlsplit(u)
        parts.port  # noqa: B018  # raises on a bad port
    except ValueError as exc:
        raise UnsafeURLError(f"ongeldige URL ({exc})") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError("alleen http:// en https:// zijn toegestaan")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("een gebruikersnaam of wachtwoord in de URL is niet toegestaan")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURLError("de URL heeft geen host")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    tld = host.rsplit(".", 1)[-1]
    if is_ip or "." not in host or not re.fullmatch(r"[a-z][a-z0-9-]*", tld):
        raise UnsafeURLError("gebruik een publieke hostnaam, geen IP-adres of lokale naam")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURLError("lokale hostnamen zijn niet toegestaan")
    return u


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None and not is_public_ip(str(embedded)):
            return False
    return bool(
        ip.is_global
        and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


async def resolve_host(host: str, port: int) -> list[str]:
    """All addresses ``host`` resolves to (tests replace this function)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_public_url(url: str) -> None:
    """Raise UnsafeURLError unless ``url`` passes :func:`validate_feed_url` and its host name
    resolves only to public addresses."""
    validate_feed_url(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        addresses = await resolve_host(host, port)
    except OSError as exc:
        raise UnsafeURLError(f"{host} is niet te resolven ({exc})") from exc
    if not addresses:
        raise UnsafeURLError(f"{host} is niet te resolven")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} wijst naar een niet-publiek adres ({bad[0]})")


class ResponseTooLarge(httpx.HTTPError):
    pass


_SECRET_PARAM_RE = re.compile(r"((?:auth_token|token|api_key|apikey|key)=)[^&\s'\"]+", re.I)


def redact(text: str) -> str:
    """Remove token query parameters from a message before it is logged or shown."""
    return _SECRET_PARAM_RE.sub(r"\1***", text)


def describe_error(exc: BaseException) -> str:
    """A log-safe description: the status code for HTTP errors, never a URL with a token."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "time-out"
    return redact(f"{type(exc).__name__}: {exc}")[:300]


# -- parsers ------------------------------------------------------------------------------


def parse_rss(content: bytes, feed_url: str, now: datetime | None = None) -> list[RawItem]:
    parsed = feedparser.parse(content)
    feed_title = _str(parsed.feed.get("title")) or feed_url
    feed_title = (_clean(feed_title, 128) or feed_url[:128]).strip()
    now = now or datetime.now(UTC)
    items: list[RawItem] = []
    for entry in parsed.entries[:MAX_ITEMS_PER_RESPONSE]:
        try:
            link = safe_url(entry.get("link"))
            title = _title(entry.get("title"))
            if not link or not title:
                continue
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            try:
                published = datetime.fromtimestamp(timegm(stamp), UTC) if stamp else now
            except (TypeError, ValueError, OverflowError, OSError):
                published = now
            items.append(
                RawItem(
                    source="rss",
                    source_name=feed_title[:128],
                    url=link,
                    title=title,
                    published_at=published,
                    summary=_clean(entry.get("summary")),
                )
            )
        except Exception as exc:  # noqa: BLE001  # one bad entry must not drop the feed
            log.warning("RSS entry in %s skipped: %s", redact(feed_url), describe_error(exc))
    return items


def parse_gdelt(payload: dict[str, Any]) -> list[RawItem]:
    items: list[RawItem] = []
    for art in _records(payload, "articles"):
        try:
            url = safe_url(art.get("url"))
            title = _title(art.get("title"))
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
                    source_name=(_clean(art.get("domain"), 128) or "gdelt"),
                    url=url,
                    title=title,
                    published_at=published,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("GDELT article skipped: %s", describe_error(exc))
    return items


def parse_cryptopanic(payload: dict[str, Any]) -> list[RawItem]:
    items: list[RawItem] = []
    for post in _records(payload, "results"):
        try:
            url = safe_url(post.get("original_url")) or safe_url(post.get("url"))
            title = _title(post.get("title"))
            if not url or not title:
                continue
            published = _iso(post.get("published_at")) or datetime.now(UTC)
            instruments = post.get("instruments") or post.get("currencies") or []
            codes = (
                [i.get("code") for i in instruments if isinstance(i, dict)]
                if isinstance(instruments, list)
                else []
            )
            source = post.get("source") if isinstance(post.get("source"), dict) else {}
            votes = post.get("votes")
            items.append(
                RawItem(
                    source="cryptopanic",
                    source_name=_clean(source.get("title"), 128) or "CryptoPanic",
                    url=url,
                    title=title,
                    published_at=published,
                    summary=_clean(post.get("description")),
                    coins=clean_tickers(codes),
                    votes=votes if isinstance(votes, dict) else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("CryptoPanic post skipped: %s", describe_error(exc))
    return items


def parse_cryptocv(payload: dict[str, Any], coin: str | None = None) -> list[RawItem]:
    """cryptocurrency.cv ``/api/archive?ticker=X`` and ``/api/news`` share one article shape."""
    items: list[RawItem] = []
    for art in _records(payload, "articles"):
        try:
            url = safe_url(art.get("canonical_link")) or safe_url(art.get("link"))
            title = _title(art.get("title"))
            if not url or not title:
                continue
            tickers = clean_tickers(art.get("tickers") or [])
            coins = tickers or clean_tickers([coin] if coin else [])
            sentiment = art.get("sentiment")
            items.append(
                RawItem(
                    source="cryptocv",
                    source_name=_clean(art.get("source"), 128) or "cryptocurrency.cv",
                    url=url,
                    title=title,
                    published_at=_iso(art.get("pub_date") or art.get("pubDate"))
                    or datetime.now(UTC),
                    summary=_clean(art.get("description")),
                    coins=coins,
                    votes=sentiment if isinstance(sentiment, dict) else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cryptocurrency.cv article skipped: %s", describe_error(exc))
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
        it.coins = clean_tickers([coin])
        head, sep, publisher = it.title.rpartition(" - ")
        if sep and head and len(publisher) <= 60:
            it.title, it.source_name = head, publisher[:128]
        else:
            it.source_name = "Google News"
    return items


# alternative.me's own bands; the label is derived from the value, never taken from the feed.
FEAR_GREED_BANDS = ((24, "Extreme Fear"), (46, "Fear"), (54, "Neutral"), (75, "Greed"))


def fear_greed_label(value: int) -> str:
    for upper, label in FEAR_GREED_BANDS:
        if value <= upper:
            return label
    return "Extreme Greed"


def parse_fear_greed(payload: dict[str, Any]) -> dict[str, Any] | None:
    """alternative.me Fear & Greed: {"value": 30, "label": "Fear", "at": "..."} or None.

    The value is clamped to 0-100 and the label derived from it.
    """
    data = _records(payload, "data")
    if not data:
        return None
    first = data[0]
    try:
        raw = float(str(first["value"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    value = int(min(max(round(raw), 0), 100))
    at = first.get("timestamp")
    try:
        when = datetime.fromtimestamp(int(at), UTC) if at else None
    except (TypeError, ValueError, OverflowError, OSError):
        when = None
    now = datetime.now(UTC)
    if when is not None and when > now:
        when = now
    return {
        "value": value,
        "label": fear_greed_label(value),
        "at": when.isoformat() if when else None,
    }


# -- fetcher ------------------------------------------------------------------------------


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
        # Redirects are followed by hand in _get, so every hop is checked.
        self._http = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with the SSRF guard per redirect hop, a byte cap and a total deadline.

        Returns a fully read response (status not checked; call ``raise_for_status``).
        """
        async with asyncio.timeout(REQUEST_DEADLINE_S):
            request = self._http.build_request("GET", url, params=params)
            for _ in range(MAX_REDIRECTS + 1):
                await check_public_url(str(request.url))
                resp = await self._http.send(request, stream=True)
                try:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise UnsafeURLError("redirect zonder Location")
                        target = resp.url.join(location)
                        if resp.url.scheme == "https" and target.scheme != "https":
                            raise UnsafeURLError("redirect van https naar http geweigerd")
                        request = self._http.build_request("GET", target)
                        continue
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                        raise ResponseTooLarge(f"antwoord groter dan {MAX_BODY_BYTES} bytes")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            raise ResponseTooLarge(f"antwoord groter dan {MAX_BODY_BYTES} bytes")
                    return httpx.Response(
                        resp.status_code,
                        headers={"content-type": resp.headers.get("content-type", "")},
                        content=bytes(body),
                        request=resp.request,
                    )
                finally:
                    await resp.aclose()
            raise UnsafeURLError(f"meer dan {MAX_REDIRECTS} redirects")

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._get(url, params)
        resp.raise_for_status()
        return await asyncio.to_thread(json.loads, resp.content)

    async def _one_feed(self, url: str) -> list[RawItem]:
        try:
            resp = await self._get(url)
            resp.raise_for_status()
            return await asyncio.to_thread(parse_rss, resp.content, url)
        except Exception as exc:  # one broken feed must not stop the rest
            log.warning("RSS feed %s failed: %s", redact(url), describe_error(exc))
            return []

    async def fetch_rss(self) -> list[RawItem]:
        results = await asyncio.gather(*(self._one_feed(url) for url in self.rss_feeds))
        return [it for items in results for it in items]

    async def fetch_gdelt(self, timespan: str = "1h", maxrecords: int = 75) -> list[RawItem]:
        if not self.gdelt_query:
            return []
        try:
            resp = await self._get(
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
            payload = await asyncio.to_thread(json.loads, resp.content)
            return await asyncio.to_thread(parse_gdelt, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("GDELT fetch failed: %s", describe_error(exc))
            return []

    async def fetch_cryptopanic(self) -> list[RawItem]:
        if not self.cryptopanic_token:
            return []
        params: dict[str, str] = {"auth_token": self.cryptopanic_token, "public": "true"}
        if self.coins:
            params["currencies"] = ",".join(self.coins)
        try:
            payload = await self._get_json(
                self.CRYPTOPANIC_URL.format(plan=self.cryptopanic_plan), params
            )
            return await asyncio.to_thread(parse_cryptopanic, payload)
        except Exception as exc:  # noqa: BLE001  # never log the URL: it carries the token
            log.warning("CryptoPanic fetch failed: %s", describe_error(exc))
            return []

    async def _cryptocv(self, url: str, params: dict[str, Any], coin: str | None) -> list:
        try:
            payload = await self._get_json(url, params)
            return await asyncio.to_thread(parse_cryptocv, payload, coin)
        except Exception as exc:  # noqa: BLE001
            what = f"archive for {coin}" if coin else "news"
            log.warning("cryptocurrency.cv %s failed: %s", what, describe_error(exc))
            return []

    async def fetch_cryptocv(self, per_coin: int = 40, general: int = 100) -> list[RawItem]:
        """Free aggregator (300+ sources): the archive per coin plus the general feed."""
        if not self.cryptocv_enabled:
            return []
        jobs = [
            self._cryptocv(self.CRYPTOCV_ARCHIVE_URL, {"ticker": coin, "limit": per_coin}, coin)
            for coin in self.coins
        ]
        jobs.append(self._cryptocv(self.CRYPTOCV_NEWS_URL, {"limit": general}, None))
        results = await asyncio.gather(*jobs)
        return [it for items in results for it in items]

    async def _google_news(self, coin: str, window: str) -> list[RawItem]:
        params = {
            "q": f"{coin_search_term(coin)} when:{window}",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        try:
            resp = await self._get(self.GOOGLE_NEWS_URL, params=params)
            resp.raise_for_status()
            return await asyncio.to_thread(parse_google_news, resp.content, str(resp.url), coin)
        except Exception as exc:  # noqa: BLE001
            log.warning("Google News for %s failed: %s", coin, describe_error(exc))
            return []

    async def fetch_google_news(self, window: str = "1d") -> list[RawItem]:
        """One Google News search feed per coin, last day."""
        if not self.google_news_enabled:
            return []
        results = await asyncio.gather(*(self._google_news(c, window) for c in self.coins))
        return [it for items in results for it in items]

    async def fetch_fear_greed(self) -> dict[str, Any] | None:
        """Market-wide Fear & Greed index (0 = extreme fear, 100 = extreme greed).

        None when the feed is off or the fetch fails: a stale value is never carried over.
        """
        if not self.fear_greed_enabled:
            self.fear_greed = None
            return None
        try:
            payload = await self._get_json(self.FEAR_GREED_URL, {"limit": 1})
            self.fear_greed = parse_fear_greed(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Fear & Greed fetch failed: %s", describe_error(exc))
            self.fear_greed = None
        return self.fear_greed

    async def _bounded(self, name: str, job) -> list[RawItem]:
        try:
            return await asyncio.wait_for(job, SOURCE_DEADLINE_S)
        except Exception as exc:  # noqa: BLE001  # a slow source must not hold the others
            log.warning("news source %s failed: %s", name, describe_error(exc))
            return []

    async def fetch_all(self) -> list[RawItem]:
        results = await asyncio.gather(
            self._bounded("rss", self.fetch_rss()),
            self._bounded("gdelt", self.fetch_gdelt()),
            self._bounded("cryptocv", self.fetch_cryptocv()),
            self._bounded("googlenews", self.fetch_google_news()),
            self._bounded("cryptopanic", self.fetch_cryptopanic()),
        )
        return [it for items in results for it in items]


def published_at(value: datetime, now: datetime) -> datetime:
    """``value`` as aware UTC, never later than ``now`` (the fetch time).

    A publisher sets its own date; a date in the future would keep an article at full weight
    (and a shock pause active) for as long as the date lies ahead.
    """
    return min(aware(value), now)
