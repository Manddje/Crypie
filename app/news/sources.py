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
import httpcore
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
URL_MAX = 1024  # a stored feed address
REQUEST_URL_MAX = 8192  # a request URL, query included (a long GDELT query is fine)


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
    feed_host: str | None = None  # RSS: host of the configured feed URL


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


def validate_feed_url(url: Any, max_length: int = URL_MAX) -> str:
    """Check a news/feed URL before it is stored or fetched; returns the stripped URL.

    Only http(s) with a dotted host name; no user:password@, no IP literal (also not in decimal
    or hex form) and no local names. Raises ValueError with a Dutch message. Whether the name
    resolves to a public address is checked at fetch time (see :func:`check_public_url`).
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("lege URL")
    u = url.strip()
    if len(u) > max_length:
        raise UnsafeURLError(f"URL is langer dan {max_length} tekens")
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
    validate_feed_url(url, REQUEST_URL_MAX)
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


class VettedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects only to addresses it resolved and checked itself.

    :func:`check_public_url` resolves the host before a request, but the connection would
    resolve it again: a rebinding DNS answer (public first, then 169.254.169.254 or a Docker
    address) would pass the check and reach the internal address. Here the connection goes to
    the vetted IP; TLS (SNI, certificate) and the Host header keep the original name.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await resolve_host(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"{host} is niet te resolven ({exc})") from exc
        bad = [a for a in addresses if not is_public_ip(a)]
        if not addresses or bad:
            shown = bad[0] if bad else "geen adres"
            raise httpcore.ConnectError(f"{host} wijst naar een niet-publiek adres ({shown})")
        last: Exception | None = None
        for address in dict.fromkeys(addresses):
            try:
                return await self._inner.connect_tcp(
                    address.split("%", 1)[0],
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets zijn niet toegestaan")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def vetted_transport() -> httpx.AsyncHTTPTransport:
    """httpx's transport, with a connection pool that connects through VettedNetworkBackend."""
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001 - no public hook in httpx
        ssl_context=httpx.create_ssl_context(),
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=5.0,
        network_backend=VettedNetworkBackend(),
    )
    return transport


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
    try:
        feed_host = (urlsplit(feed_url).hostname or "").rstrip(".").lower()[:255] or None
    except ValueError:
        feed_host = None
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
                    feed_host=feed_host,
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


# -- ETF flows (TFTC, cryptoetf.today) ---------------------------------------------------------

# Coins with US spot ETFs, as cryptoetf.today names them in its path (/flows/<asset>).
ETF_ASSETS = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
ETF_FLOW_DAYS = 30  # the daily net flows kept per coin (the summary uses them all)
ETF_FLOW_RECENT_DAYS = 5  # the window of the label (about one trading week)
ETF_FLOW_MIN_INTERVAL_S = 3600  # the data changes once a trading day; ask at most hourly
# The label compares the net flow of the last 5 days with what a day typically moves: a week
# of net inflow equal to half the usual daily movement is "strong".
ETF_FLOW_STRONG = 0.5
ETF_FLOW_MILD = 0.15


def etf_flow_label(sum_recent: float, typical_abs_day: float, days: int) -> str:
    """strong_inflow | inflow | mixed | outflow | strong_outflow, or unknown without data."""
    if days <= 0 or not typical_abs_day > 0:
        return "unknown"
    ratio = sum_recent / (days * typical_abs_day)
    if ratio >= ETF_FLOW_STRONG:
        return "strong_inflow"
    if ratio >= ETF_FLOW_MILD:
        return "inflow"
    if ratio <= -ETF_FLOW_STRONG:
        return "strong_outflow"
    if ratio <= -ETF_FLOW_MILD:
        return "outflow"
    return "mixed"


def _etf_date(value: Any, now: datetime) -> str | None:
    """An ISO date (YYYY-MM-DD) that is not in the future, else None."""
    text = _str(value)
    if not text:
        return None
    try:
        day = datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None
    return day.isoformat() if day <= now.date() else None


def _musd(value: Any, scale: float = 1e6) -> float | None:
    """A finite amount as millions of USD, rounded to 0.1; None when unusable."""
    try:
        amount = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return round(amount / scale, 1)


def _tftc_day(row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """One TFTC session: {"date": "2026-11-04", "netFlowUsd": ..., "perEtfUsd": {...}}."""
    date = _etf_date(row.get("date"), now)
    flow = _musd(row.get("netFlowUsd", row.get("vendorNetFlowUsd")))
    if date is None or flow is None:
        return None
    funds: list[dict[str, Any]] = []
    per_fund = row.get("perEtfUsd")
    if isinstance(per_fund, dict):
        for ticker, amount in list(per_fund.items())[:MAX_ITEMS_PER_RESPONSE]:
            fund_flow = _musd(amount)
            if fund_flow and isinstance(ticker, str) and ticker.strip():
                clean = re.sub(r"[^A-Z0-9.]", "", ticker.upper())[:12]
                if clean:
                    funds.append({"ticker": clean, "flow_musd": fund_flow})
    funds.sort(key=lambda f: -abs(f["flow_musd"]))
    price = _musd(row.get("btcCloseUsd"), scale=1)
    return {"date": date, "flow_musd": flow, "price_usd": price, "funds": funds[:5]}


def _cryptoetf_day(row: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """One cryptoetf.today day: {"date": "2026-11-04", "netFlowUsdM": 74.6} (millions)."""
    date = _etf_date(row.get("date"), now)
    flow = _musd(row.get("netFlowUsdM"), scale=1)
    if date is None or flow is None:
        return None
    return {"date": date, "flow_musd": flow, "price_usd": None, "funds": []}


def summarise_etf_flows(
    days: list[dict[str, Any]], coin: str, source: str
) -> dict[str, Any] | None:
    """The compact summary Claude and the page get, from normalised day rows (any order).

    Net flows in millions of USD (positive = money into the ETFs) over the last ETF_FLOW_DAYS
    days, newest last; the label follows from the last ETF_FLOW_RECENT_DAYS days against the
    typical daily movement over the whole window.
    """
    by_date = {d["date"]: d for d in days}  # a later row for the same day replaces the earlier
    if not by_date:
        return None
    kept = [by_date[d] for d in sorted(by_date)][-ETF_FLOW_DAYS:]
    recent = kept[-ETF_FLOW_RECENT_DAYS:]
    flows = [d["flow_musd"] for d in kept]
    typical = sum(abs(f) for f in flows) / len(flows)
    sum_recent = sum(d["flow_musd"] for d in recent)
    streak = 0
    for d in reversed(kept):
        if d["flow_musd"] == 0 or (streak and (d["flow_musd"] > 0) != (streak > 0)):
            break
        streak += 1 if d["flow_musd"] > 0 else -1
    last = kept[-1]
    return {
        "coin": coin.upper(),
        "as_of": last["date"],
        "last_day_musd": last["flow_musd"],
        "last_day_funds": last["funds"],
        "sum_5d_musd": round(sum_recent, 1),
        "sum_30d_musd": round(sum(flows), 1),
        "typical_day_musd": round(typical, 1),
        "streak_days": streak,
        "days": len(kept),
        "label": etf_flow_label(sum_recent, typical, len(recent)),
        "daily": [{"date": d["date"], "flow_musd": d["flow_musd"]} for d in kept],
        "price_usd": last["price_usd"],
        "source": source,
    }


def parse_etf_flows_tftc(payload: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """TFTC's open Bitcoin ETF series (data.json, CC BY 4.0): per-fund flows in USD."""
    now = now or datetime.now(UTC)
    rows = [_tftc_day(r, now) for r in _records(payload, "days")]
    return summarise_etf_flows([r for r in rows if r], "BTC", "tftc")


def parse_etf_flows_cryptoetf(
    payload: Any, coin: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """cryptoetf.today's /flows/<asset>: 30 days of net flows in millions, no fund breakdown."""
    now = now or datetime.now(UTC)
    rows = [_cryptoetf_day(r, now) for r in _records(payload, "days")]
    return summarise_etf_flows([r for r in rows if r], coin, "cryptoetf.today")


# -- fetcher ------------------------------------------------------------------------------


class NewsFetcher:
    GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
    CRYPTOPANIC_URL = "https://cryptopanic.com/api/{plan}/v2/posts/"
    CRYPTOCV_ARCHIVE_URL = "https://cryptocurrency.cv/api/archive"
    CRYPTOCV_NEWS_URL = "https://cryptocurrency.cv/api/news"
    GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
    FEAR_GREED_URL = "https://api.alternative.me/fng/"
    # The last 30 days per coin, with a free key (no card); 60 requests a minute.
    CRYPTOETF_URL = "https://api.cryptoetf.today/api/v1/flows/{asset}"
    # Bitcoin only, no key: the full daily series since launch (about 200 kB), CC BY 4.0.
    TFTC_URL = "https://www.tftc.io/bitcoin-etf-flows/data.json"

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
        cryptoetf_api_key: str = "",
        etf_flows_enabled: bool = True,
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
        self.cryptoetf_api_key = cryptoetf_api_key
        self.etf_flows_enabled = etf_flows_enabled
        # ETF flows per coin from the last fetch, and when; see fetch_etf_flows.
        self.etf_flows: dict[str, dict[str, Any] | None] = {}
        self._etf_flows_at: float | None = None
        self._plain_http_warned: set[str] = set()
        # Redirects are followed by hand in _get, so every hop is checked.
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            transport=vetted_transport(),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with the SSRF guard per redirect hop, a byte cap and a total deadline.

        Returns a fully read response (status not checked; call ``raise_for_status``).
        ``headers`` (an API key) go to the first hop only, never along a redirect.
        """
        async with asyncio.timeout(REQUEST_DEADLINE_S):
            request = self._http.build_request("GET", url, params=params, headers=headers)
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

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await self._get(url, params, headers)
        resp.raise_for_status()
        return await asyncio.to_thread(json.loads, resp.content)

    async def _one_feed(self, url: str) -> list[RawItem]:
        if url[:7].lower() == "http://" and url not in self._plain_http_warned:
            # Stored before https was required: still read, but say so once.
            self._plain_http_warned.add(url)
            log.warning(
                "RSS feed %s uses plain http: its content can be changed on the way; "
                "switch it to https in the settings",
                redact(url),
            )
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

    async def _etf_flows_for(self, coin: str, now: datetime) -> dict[str, Any] | None:
        """One coin: cryptoetf.today with a key, else TFTC for bitcoin; None when it fails."""
        try:
            if self.cryptoetf_api_key:
                payload = await self._get_json(
                    self.CRYPTOETF_URL.format(asset=ETF_ASSETS[coin]),
                    headers={"Authorization": f"Bearer {self.cryptoetf_api_key}"},
                )
                return await asyncio.to_thread(parse_etf_flows_cryptoetf, payload, coin, now)
            payload = await self._get_json(self.TFTC_URL)
            return await asyncio.to_thread(parse_etf_flows_tftc, payload, now)
        except Exception as exc:  # noqa: BLE001  # never log the headers: they carry the key
            log.warning("ETF flows for %s failed: %s", coin, describe_error(exc))
            return None

    def etf_flow_coins(self) -> list[str]:
        """The configured coins the flows can be fetched for: every coin with a US spot ETF
        with a cryptoetf.today key, only bitcoin (TFTC) without one."""
        if not self.etf_flows_enabled:
            return []
        if self.cryptoetf_api_key:
            return [c for c in self.coins if c in ETF_ASSETS]
        return [c for c in self.coins if c == "BTC"]

    async def fetch_etf_flows(self) -> dict[str, dict[str, Any] | None]:
        """Daily net flows of the US spot ETFs per configured coin that has them.

        {} when the source is off (or no coin can be served). A coin maps to None when its
        fetch failed (the stored value then stays until it is too old). Asked at most once an
        hour: the figures change once a trading day.
        """
        coins = self.etf_flow_coins()
        if not coins:
            self.etf_flows = {}
            self._etf_flows_at = None
            return {}
        now = datetime.now(UTC)
        fresh = (
            self._etf_flows_at is not None
            and now.timestamp() - self._etf_flows_at < ETF_FLOW_MIN_INTERVAL_S
            and set(self.etf_flows) == set(coins)
        )
        if fresh:
            return self.etf_flows
        results = await asyncio.gather(*(self._etf_flows_for(c, now) for c in coins))
        self.etf_flows = dict(zip(coins, results, strict=True))
        self._etf_flows_at = now.timestamp()
        return self.etf_flows

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
