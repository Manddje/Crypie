"""Glue: store fetched items, run assessments, aggregate market sentiment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import anthropic
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded, prune_usage
from app.ai.news_analyst import NewsAnalyst, apply_assessments, normalize_key
from app.db import NewsItem, Setting
from app.news.registry import register_items
from app.news.sources import (
    ETF_ASSETS,
    MAX_VOTES_JSON,
    NewsFetcher,
    RawItem,
    aware,
    clean_tickers,
    describe_error,
    etf_flow_label,
    fear_greed_label,
    published_at,
    safe_url,
)

log = logging.getLogger(__name__)

TITLE_MAX = 500
SUMMARY_MAX = 2000


def _row_for(r: RawItem, now: datetime) -> dict[str, Any] | None:
    """Column values for one fetched item, validated and bounded to the columns; None to skip."""
    url = safe_url(r.url)
    title = r.title.strip()[:TITLE_MAX] if isinstance(r.title, str) else ""
    if not url or not title or not isinstance(r.published_at, datetime):
        return None
    votes = None
    if isinstance(r.votes, dict):
        votes = json.dumps(r.votes, default=str)
        if len(votes) > MAX_VOTES_JSON:
            votes = None
    source_name = r.source_name.strip() if isinstance(r.source_name, str) else ""
    return {
        "source": (r.source if isinstance(r.source, str) else "")[:32] or "rss",
        "source_name": source_name[:128] or "?",
        "url": url,
        "title": title,
        "summary": r.summary[:SUMMARY_MAX] if isinstance(r.summary, str) and r.summary else None,
        "published_at": published_at(r.published_at, now),
        "source_coins": ",".join(clean_tickers(r.coins))[:128] or None,
        "source_votes": votes,
        "feed_host": r.feed_host[:255] if isinstance(r.feed_host, str) and r.feed_host else None,
    }


def store_items(
    session: Session, raw: list[RawItem], max_age_hours: int, now: datetime | None = None
) -> int:
    """Insert unseen items (by URL). Returns the number inserted.

    Every item is validated on its own: a bad one is logged and skipped, never fatal for the
    rest. Publication dates become aware UTC and never lie after the fetch time.
    """
    if not raw:
        return 0
    now = now or datetime.now(UTC)
    # Never older than the retention: such items would be pruned and fetched again each run.
    cutoff = now - timedelta(hours=min(max_age_hours, NEWS_RETENTION_DAYS * 24))
    fresh: dict[str, dict[str, Any]] = {}
    for r in raw:
        try:
            row = _row_for(r, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("news item skipped: %s", describe_error(exc))
            continue
        if row is None or row["published_at"] < cutoff:
            continue
        fresh.setdefault(row["url"], row)
    if not fresh:
        return 0
    existing = set(session.scalars(select(NewsItem.url).where(NewsItem.url.in_(list(fresh)))).all())
    rows = [row for url, row in fresh.items() if url not in existing]
    if not rows:
        return 0
    try:
        session.add_all([NewsItem(**row) for row in rows])
        session.commit()
        return len(rows)
    except SQLAlchemyError as exc:
        session.rollback()
        log.warning("news batch insert failed (%s); inserting one by one", describe_error(exc))
    inserted = 0
    for row in rows:
        try:
            session.add(NewsItem(**row))
            session.commit()
            inserted += 1
        except SQLAlchemyError as exc:
            session.rollback()
            log.warning("news item %s skipped: %s", row["url"][:200], describe_error(exc))
    return inserted


def backfill_feed_hosts(session: Session) -> int:
    """Give RSS rows stored before feed_host existed the host of their feed, so one feed is one
    outlet across the upgrade (old rows were keyed by the feed title, new ones by the host).

    Copied from newer rows of the same feed title, and only when that title maps to exactly one
    host. Returns the number of rows filled; a no-op once the old rows have been pruned.
    """
    old_titles = (
        select(NewsItem.source_name)
        .where(NewsItem.source == "rss", NewsItem.feed_host.is_(None))
        .distinct()
    )
    hosts: dict[str, set[str]] = {}
    for name, host in session.execute(
        select(NewsItem.source_name, NewsItem.feed_host)
        .where(
            NewsItem.source == "rss",
            NewsItem.feed_host.is_not(None),
            NewsItem.source_name.in_(old_titles),
        )
        .distinct()
    ):
        hosts.setdefault(name, set()).add(host)
    filled = 0
    for name, found in hosts.items():
        if len(found) != 1:
            continue  # two feeds with one title: leave the old rows keyed by the title
        filled += session.execute(
            update(NewsItem)
            .where(
                NewsItem.source == "rss",
                NewsItem.feed_host.is_(None),
                NewsItem.source_name == name,
            )
            .values(feed_host=next(iter(found)))
            .execution_options(synchronize_session=False)
        ).rowcount
    if filled:
        session.commit()
        log.info("news: %d older RSS items linked to their feed host", filled)
    return filled


def clamp_future_items(session: Session, now: datetime | None = None) -> int:
    """Pull stored items dated in the future back to their fetch time (rows from before the
    ingestion clamp existed)."""
    now = now or datetime.now(UTC)
    rows = session.scalars(select(NewsItem).where(NewsItem.published_at > now)).all()
    for it in rows:
        fetched = aware(it.fetched_at) if it.fetched_at else now
        it.published_at = min(fetched, now)
    if rows:
        session.commit()
    return len(rows)


NEWS_RETENTION_DAYS = 30  # far beyond the 48-hour windows and the 3 days on /news


def prune_old_news(session: Session, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=NEWS_RETENTION_DAYS)
    # No in-session evaluation: SQLite hands back naive datetimes, the cutoff is aware.
    stmt = delete(NewsItem).where(NewsItem.published_at < cutoff)
    result = session.execute(stmt.execution_options(synchronize_session=False))
    session.commit()
    return int(result.rowcount or 0)


MARKET_MOOD_KEY = "market_mood"
MARKET_MOOD_MAX_AGE = timedelta(hours=48)


def save_market_mood(session: Session, mood: dict) -> None:
    """Store the Fear & Greed index. The value it replaces is kept as ``previous`` when it is
    from an earlier day (the index changes once a day), for "yesterday" in the shock check."""
    row = session.get(Setting, MARKET_MOOD_KEY)
    mood = dict(mood)
    if row is not None:
        try:
            old = json.loads(row.value)
        except ValueError:
            old = {}
        if isinstance(old, dict) and "value" in old:
            if old.get("at") and mood.get("at") and old["at"] != mood["at"]:
                mood["previous"] = {"value": old["value"], "at": old["at"]}
            elif isinstance(old.get("previous"), dict):
                mood["previous"] = old["previous"]
    payload = json.dumps(mood)
    if row is None:
        session.add(Setting(key=MARKET_MOOD_KEY, value=payload))
    else:
        row.value = payload
        row.updated_at = datetime.now(UTC)
    session.commit()


def clear_market_mood(session: Session) -> None:
    row = session.get(Setting, MARKET_MOOD_KEY)
    if row is not None:
        session.delete(row)
        session.commit()


def load_market_mood(session: Session, now: datetime | None = None) -> dict | None:
    """The stored Fear & Greed index, or None when there is none, it is unreadable or older
    than 48 hours. The value is clamped to 0-100 and the label derived from it."""
    row = session.get(Setting, MARKET_MOOD_KEY)
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        raw = float(data["value"])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(raw):
        return None
    value = int(min(max(round(raw), 0), 100))
    at = data.get("at")
    when = None
    if isinstance(at, str):
        try:
            when = aware(datetime.fromisoformat(at))
        except ValueError:
            when = None
    if when is None and row.updated_at is not None:
        when = aware(row.updated_at)
    now = now or datetime.now(UTC)
    if when is None or now - when > MARKET_MOOD_MAX_AGE:
        return None
    return {"value": value, "label": fear_greed_label(value), "at": at}


def load_market_mood_previous(session: Session, now: datetime | None = None) -> dict | None:
    """The Fear & Greed value before the current one (normally yesterday's), or None when it is
    not known or older than 72 hours."""
    row = session.get(Setting, MARKET_MOOD_KEY)
    if row is None:
        return None
    try:
        prev = json.loads(row.value).get("previous")
        raw = float(prev["value"])
        when = aware(datetime.fromisoformat(prev["at"]))
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    now = now or datetime.now(UTC)
    if not math.isfinite(raw) or now - when > timedelta(hours=72):
        return None
    value = int(min(max(round(raw), 0), 100))
    return {"value": value, "label": fear_greed_label(value), "at": prev["at"]}


ETF_FLOWS_KEY_PREFIX = "etf_flows:"
# Flows are published once per US trading day; Friday's figure must still count on Monday
# (a long weekend included), and a value older than that says nothing about now.
ETF_FLOWS_MAX_AGE = timedelta(days=5)

# The labels of parse_etf_flows, as the news page says them (stored and in the API in English,
# like the Fear & Greed label).
ETF_FLOW_NL = {
    "strong_inflow": "sterke instroom",
    "inflow": "instroom",
    "mixed": "gemengd",
    "outflow": "uitstroom",
    "strong_outflow": "sterke uitstroom",
    "unknown": "onbekend",
}


def save_etf_flows(session: Session, flows: dict) -> None:
    """Store the ETF-flow summary of one coin (``flows["coin"]``), replacing the previous one."""
    key = ETF_FLOWS_KEY_PREFIX + str(flows["coin"]).upper()
    row = session.get(Setting, key)
    payload = json.dumps(flows)
    if row is None:
        session.add(Setting(key=key, value=payload))
    else:
        row.value = payload
        row.updated_at = datetime.now(UTC)
    session.commit()


def clear_etf_flows(session: Session, coins: list[str] | None = None) -> None:
    """Forget the stored flows of ``coins`` (None: every coin)."""
    rows = session.execute(
        select(Setting).where(Setting.key.like(ETF_FLOWS_KEY_PREFIX + "%"))
    ).scalars()
    wanted = None if coins is None else {c.upper() for c in coins}
    for row in rows:
        if wanted is None or row.key[len(ETF_FLOWS_KEY_PREFIX) :] in wanted:
            session.delete(row)
    session.commit()


def load_etf_flows(session: Session, coin: str, now: datetime | None = None) -> dict | None:
    """The stored ETF-flow summary of ``coin``, or None when there is none, it is unreadable
    or its last trading day is more than ETF_FLOWS_MAX_AGE ago. The label is derived again
    from the figures, never trusted from the store."""
    row = session.get(Setting, ETF_FLOWS_KEY_PREFIX + coin.upper())
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        as_of = datetime.fromisoformat(str(data["as_of"])).replace(tzinfo=UTC)
        sum_recent = float(data["sum_5d_musd"])
        typical = float(data["typical_day_musd"])
        days = int(data.get("days") or 0)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if not (math.isfinite(sum_recent) and math.isfinite(typical)):
        return None
    now = now or datetime.now(UTC)
    if now - as_of > ETF_FLOWS_MAX_AGE:
        return None
    data["label"] = etf_flow_label(sum_recent, typical, min(days, 5))
    data["label_nl"] = ETF_FLOW_NL[data["label"]]
    data["note"] = (
        "dagelijkse nettostroom van de Amerikaanse spot-ETF's in miljoenen USD (positief = "
        "geld erin); context, geen signaal"
    )
    return data


def load_all_etf_flows(
    session: Session, coins: list[str], now: datetime | None = None
) -> dict[str, dict | None]:
    """Per coin with an ETF: its stored summary (or None); coins without an ETF are left out."""
    return {c: load_etf_flows(session, c, now) for c in coins if c.upper() in ETF_ASSETS}


# alternative.me's bands, as the news page says them (the stored label stays English: Claude
# and the API get the source's own words).
FEAR_GREED_NL = {
    "Extreme Fear": "Extreme angst",
    "Fear": "Angst",
    "Neutral": "Neutraal",
    "Greed": "Hebzucht",
    "Extreme Greed": "Extreme hebzucht",
}


def pending_items(session: Session, limit: int, since: datetime | None = None) -> list[NewsItem]:
    """Unassessed items: never-tried ones first, newest fetched first."""
    stmt = select(NewsItem).where(NewsItem.assessed.is_(False))
    if since is not None:
        stmt = stmt.where(NewsItem.published_at >= since)
    return list(
        session.scalars(
            stmt.order_by(
                func.coalesce(NewsItem.assess_attempts, 0).asc(),
                NewsItem.fetched_at.desc(),
                NewsItem.id.desc(),
            ).limit(limit)
        )
    )


def recent_items(
    session: Session,
    hours: int = 24,
    limit: int = 100,
    before: tuple[datetime, int] | None = None,
) -> list[NewsItem]:
    """Articles of the last ``hours``, newest first; ``before`` = (published_at, id) of the
    last one of the previous page."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    stmt = select(NewsItem).where(NewsItem.published_at >= cutoff, NewsItem.published_at <= now)
    if before is not None:
        at, item_id = before
        stmt = stmt.where(
            or_(
                NewsItem.published_at < at,
                and_(NewsItem.published_at == at, NewsItem.id < item_id),
            )
        )
    return list(
        session.scalars(
            stmt.order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(limit)
        )
    )


def count_items(session: Session, hours: int, now: datetime | None = None) -> int:
    """How many articles were published in the last ``hours`` (the whole window, not a page)."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    return int(
        session.scalar(
            select(func.count(NewsItem.id)).where(
                NewsItem.published_at >= cutoff, NewsItem.published_at <= now
            )
        )
        or 0
    )


# Function words that tell an English headline from a Dutch one. Words both languages use
# ('in', 'is') are left out. Only a hint for screen readers (lang="en"), never for logic.
_EN_WORDS = frozenset(
    "the a an of to and for on with as by after from will its are at says new over amid into "
    "this that has have was were be been than but not you your how why what who".split()
)
_NL_WORDS = frozenset(
    "de het een van en op met voor naar niet dat bij uit zijn wordt worden ook door maar nog "
    "deze die dan al wel om tot over aan heeft hebben was waren".split()
)
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def text_lang(text: str | None) -> str:
    """'en' or 'nl' for a headline: Dutch only when its Dutch function words outnumber the
    English ones. The default feeds are English, so a headline without clues counts as English."""
    words = _WORD.findall((text or "").lower())
    en = sum(1 for w in words if w in _EN_WORDS)
    nl = sum(1 for w in words if w in _NL_WORDS)
    return "nl" if nl > en else "en"


def event_key_for(item: NewsItem) -> str:
    """Claude's event key, or the normalised title for items assessed before keys existed."""
    return item.event_key or ("title:" + normalize_key(item.title))


_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def outlet_name(name: str | None) -> str:
    """A publisher name as an outlet key: lower-case letters and digits ("GlobeNewswire",
    "The Block" -> "globenewswire", "theblock")."""
    return _NOT_ALNUM.sub("", (name or "").lower()) or "?"


def outlet_host(host: str | None) -> str:
    """A host as an outlet key: the main label of its registrable domain, so subdomains and
    country domains of one publisher meet its name ("globenewswire.com", "bbc.co.uk" ->
    "globenewswire", "bbc")."""
    labels = [x for x in (host or "").lower().rstrip(".").split(".") if x]
    if len(labels) < 2:
        return outlet_name(labels[0] if labels else "")
    second_level = len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL
    return outlet_name(labels[-3] if second_level else labels[-2])


def outlet_of(item: NewsItem) -> str:
    """Who published an article, as one key for every source, so "two independent outlets"
    means the same everywhere (sentiment, /news, the shock pause, news entries and exits).

    RSS: the feed itself (its site; its title for older rows), never the hosts its items link
    to (one hostile feed can link anywhere). Google News: the publisher's name (its links all
    point at news.google.com). Otherwise the article's site. Names and sites share one key
    space, so one publisher reached through two aggregators counts once."""
    if item.source == "rss":
        # Rows stored before feed_host existed: the feed's own title, constant per feed.
        return outlet_host(item.feed_host) if item.feed_host else outlet_name(item.source_name)
    host = (urlsplit(item.url or "").hostname or "").lower()
    if item.source == "googlenews" or not host or host.endswith("news.google.com"):
        return outlet_name(item.source_name)
    return outlet_host(host)


# One outlet alone cannot make a news fact "groot" (impact 3), nor make a market-wide ("ALL")
# claim count for every coin: that needs a second, independent outlet.
MIN_CONFIRMING_OUTLETS = 2
UNCONFIRMED_MAX_IMPACT = 2


def _lead(members: list[NewsItem]) -> NewsItem:
    """The article shown for a news fact: the first one we saw (not the most dramatic, and
    not the one that backdates itself: a publisher sets its own date, we set fetched_at)."""
    return min(members, key=lambda m: (_fetched(m), _published(m), m.id or 0))


def _outlet_mean_sentiment(members: list[NewsItem]) -> float | None:
    """Mean sentiment with one vote per outlet (the mean of that outlet's articles)."""
    per_outlet: dict[str, list[float]] = {}
    for m in members:
        if m.sentiment is not None:
            per_outlet.setdefault(outlet_of(m), []).append(m.sentiment)
    if not per_outlet:
        return None
    return sum(sum(v) / len(v) for v in per_outlet.values()) / len(per_outlet)


def _event_impact(members: list[NewsItem]) -> int | None:
    impacts = [m.impact for m in members if m.impact is not None]
    if not impacts:
        return None
    impact = max(impacts)
    if len({outlet_of(m) for m in members}) < MIN_CONFIRMING_OUTLETS:
        impact = min(impact, UNCONFIRMED_MAX_IMPACT)
    return impact


def requeue_items_without_event_key(session: Session, hours: int = 48) -> int:
    """Send relevant items assessed before event keys existed back to Claude once.

    Only items that still count in the sentiment (relevant, recent) are re-queued; irrelevant
    ones never need a key. Idempotent: after re-assessment every item has a key.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    rows = session.scalars(
        select(NewsItem).where(
            NewsItem.assessed.is_(True),
            NewsItem.relevant.is_(True),
            NewsItem.event_key.is_(None),
            NewsItem.published_at >= cutoff,
        )
    ).all()
    for it in rows:
        it.assessed = False
    session.commit()
    return len(rows)


SENTIMENT_WINDOW_H = 48  # the engine and /news weigh news of the last 48 hours


@dataclass(frozen=True)
class CountStatus:
    """Whether an article weighs in the market sentiment, and why (not), in Dutch."""

    counts: bool
    code: str  # counted | pending | irrelevant | too_old | other_coins | no_impact | unconfirmed
    reason_nl: str
    coins: tuple[str, ...] = ()  # the tracked coins this article weighs for

    def detail_nl(self, assessing: bool = True) -> str:
        """The reason for use next to a 'telt mee' / 'telt niet mee' label, without repeating
        it: 'voor BTC · sentiment gemiddeld over 2 bronnen'. Without a Claude key nothing is
        waiting for an assessment, so that is said instead of 'nog niet'."""
        if self.code == "pending" and not assessing:
            return "niet beoordeeld (geen Claude-sleutel)"
        if self.code == "no_impact":
            return "impact 0: weegt niet mee in het sentiment"
        text = self.reason_nl
        if text.startswith("telt mee "):
            text = text[len("telt mee ") :]
        return text.replace(", hoogste impact", "")


def count_status(
    item: NewsItem,
    tracked: list[str],
    now: datetime | None = None,
    hours: int = SENTIMENT_WINDOW_H,
) -> CountStatus:
    """Mirror the filters of :func:`market_sentiment` for one article, with a reason."""
    now = now or datetime.now(UTC)
    if not item.assessed:
        return CountStatus(False, "pending", "nog niet beoordeeld door Claude")
    if not item.relevant:
        return CountStatus(False, "irrelevant", "volgens Claude niet koersrelevant")
    if _published(item) < now - timedelta(hours=hours):
        return CountStatus(False, "too_old", f"ouder dan {hours} uur")
    coins = item.coin_list()
    hits = tuple(c for c in tracked if c in coins or "ALL" in coins)
    if not hits:
        about = ", ".join(c for c in coins if c != "ALL") or "geen coin"
        return CountStatus(False, "other_coins", f"gaat over {about}, niet over jouw coins")
    if not item.impact or item.sentiment is None:
        return CountStatus(False, "no_impact", "impact 0: weegt niet mee")
    return CountStatus(True, "counted", "telt mee voor " + ", ".join(hits), hits)


@dataclass
class NewsGroup:
    """Articles about one news fact, for the /news list: the lead plus the other outlets."""

    key: str
    lead: NewsItem  # the first article published
    items: list[NewsItem]  # all articles, newest first
    status: CountStatus | None = None  # set by :func:`annotate_groups`

    @property
    def sources(self) -> int:
        """Distinct outlets that ran this fact (several articles of one site count once)."""
        return len({outlet_of(it) for it in self.items})

    @property
    def others(self) -> list[NewsItem]:
        return [it for it in self.items if it is not self.lead]

    @property
    def anchor(self) -> str:
        """A stable id for the fact on the page ('feit-1a2b3c4d5e'), so the coin cards can link
        to it."""
        return fact_anchor(self.key)

    @property
    def assessed(self) -> bool:
        return any(it.assessed for it in self.items)

    @property
    def relevant(self) -> bool:
        return any(it.relevant for it in self.items if it.assessed)

    @property
    def sentiment(self) -> float | None:
        return _outlet_mean_sentiment([it for it in self.items if it.relevant])

    @property
    def impact(self) -> int | None:
        return _event_impact([it for it in self.items if it.relevant])

    def coin_list(self) -> list[str]:
        coins: list[str] = []
        for it in self.items:
            for c in it.coin_list():
                if c not in coins:
                    coins.append(c)
        return coins

    def count_status(self, tracked: list[str], now: datetime | None = None) -> CountStatus:
        """The fact counts when any of its articles does; otherwise the lead's reason."""
        statuses = [count_status(it, tracked, now) for it in self.items]
        counting = [st for st in statuses if st.counts]
        if not counting:
            return statuses[self.items.index(self.lead)]
        explicit = {c for it in self.items for c in it.coin_list() if c != "ALL"}
        if not explicit & set(tracked) and self.sources < MIN_CONFIRMING_OUTLETS:
            return CountStatus(
                False,
                "unconfirmed",
                "marktbreed nieuws uit één bron: telt pas mee als een tweede bron het meldt",
            )
        coins = tuple(dict.fromkeys(c for st in counting for c in st.coins))
        if self.sources < MIN_CONFIRMING_OUTLETS:
            coins = tuple(c for c in coins if c in explicit)
        reason = "telt mee voor " + ", ".join(coins)
        if self.sources > 1 and len(counting) > 1:
            reason += f" · sentiment gemiddeld over {self.sources} bronnen, hoogste impact"
        return CountStatus(True, "counted", reason, coins)


def fact_anchor(key: str) -> str:
    return "feit-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def group_items(items: list[NewsItem]) -> list[NewsGroup]:
    """Fold a newest-first list of articles into one group per news fact, newest fact first."""
    groups: dict[str, list[NewsItem]] = {}
    for it in items:
        groups.setdefault(event_key_for(it), []).append(it)
    return [NewsGroup(key, _lead(members), members) for key, members in groups.items()]


def annotate_groups(
    groups: list[NewsGroup],
    tracked: list[str],
    counted: bool | None = None,
    now: datetime | None = None,
) -> list[NewsGroup]:
    """Set ``status`` on every group and ``counting`` on every article; filter on ``counted``.

    ``counted=None`` keeps all groups, ``True`` only facts that weigh in the sentiment, ``False``
    only facts that do not.
    """
    now = now or datetime.now(UTC)
    out = []
    for g in groups:
        for it in g.items:
            it.counting = count_status(it, tracked, now)  # type: ignore[attr-defined]
            it.lang = text_lang(it.title)  # type: ignore[attr-defined]
        g.status = g.count_status(tracked, now)
        if counted is None or g.status.counts == counted:
            out.append(g)
    return out


def recent_event_keys(
    session: Session, hours: int = 48, limit: int = 40, now: datetime | None = None
) -> list[str]:
    """Most-used event keys of the last ``hours``, so Claude reuses them for new items."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    rows = session.execute(
        select(NewsItem.event_key, func.count(NewsItem.id))
        .where(
            NewsItem.event_key.is_not(None),
            NewsItem.published_at >= cutoff,
            NewsItem.published_at <= now,
        )
        .group_by(NewsItem.event_key)
        .order_by(func.count(NewsItem.id).desc())
        .limit(limit)
    ).all()
    return [key for key, _ in rows]


@dataclass
class MarketSentiment:
    coin: str
    score: float  # -1 .. 1, impact-weighted and time-decayed
    items: int
    high_impact: int
    top: list[NewsItem]  # the lead article per news fact; ``sources`` = how many outlets ran it
    articles: int = 0  # ``items`` counts news facts; this counts the articles behind them
    # High-impact facts reported by at least two independent outlets.
    confirmed_high_impact: int = 0

    def label(self) -> str:
        if self.items == 0:
            return "geen nieuws"
        if self.score >= 0.4:
            return "positief"
        if self.score >= 0.15:
            return "licht positief"
        if self.score <= -0.4:
            return "negatief"
        if self.score <= -0.15:
            return "licht negatief"
        return "neutraal"

    def as_dict(self) -> dict:
        return {
            "coin": self.coin,
            "score": round(self.score, 3),
            "label": self.label(),
            "items": self.items,
            "articles": self.articles,
            "high_impact": self.high_impact,
            "confirmed_high_impact": self.confirmed_high_impact,
            "top": [
                {
                    "title": t.title,
                    "sources": getattr(t, "sources", 1),
                    "summary_nl": t.summary_nl,
                    "sentiment": getattr(t, "event_sentiment", t.sentiment),
                    "impact": getattr(t, "event_impact", t.impact),
                    "url": t.url,
                    "published_at": t.published_at,
                }
                for t in self.top
            ],
        }


HORIZON_HALF_LIFE_H = {"hours": 6.0, "days": 36.0, "weeks": 168.0}


def market_sentiment(
    session: Session,
    coin: str,
    hours: int = SENTIMENT_WINDOW_H,
    now: datetime | None = None,
) -> MarketSentiment:
    """Impact-weighted, time-decayed average sentiment of relevant news for one coin.

    Articles about the same news fact (same event key) are folded into one event first, so a
    hack that twelve outlets report weighs like one hack, not twelve. Within an event every
    outlet has one vote on the sentiment; the impact is the highest of its articles, but at most
    2 while only one outlet reports it; a market-wide ("ALL") fact needs two outlets before it
    counts for a coin it does not name. The event ages from its newest article.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    rows = session.scalars(
        select(NewsItem).where(
            NewsItem.assessed.is_(True),
            NewsItem.relevant.is_(True),
            NewsItem.published_at >= cutoff,
            NewsItem.published_at <= now,
        )
    ).all()
    events: dict[str, list[NewsItem]] = {}
    for it in rows:
        coins = it.coin_list()
        if coin not in coins and "ALL" not in coins:
            continue
        if not it.impact or it.sentiment is None:
            continue
        events.setdefault(event_key_for(it), []).append(it)

    weighted = 0.0
    total_w = 0.0
    high = 0
    confirmed_high = 0
    articles = 0
    scored: list[tuple[float, NewsItem]] = []
    for members in events.values():
        outlets = len({outlet_of(m) for m in members})
        named = any(coin in m.coin_list() for m in members)
        if not named and outlets < MIN_CONFIRMING_OUTLETS:
            continue  # a market-wide claim from a single outlet
        lead = _lead(members)
        impact = _event_impact(members) or 0
        sentiment = _outlet_mean_sentiment(members) or 0.0
        age_h = max((now - max(_published(m) for m in members)).total_seconds() / 3600, 0.0)
        half_life = HORIZON_HALF_LIFE_H.get(lead.horizon or "days", 36.0)
        decay = math.exp(-math.log(2) * age_h / half_life)
        w = impact * decay
        weighted += sentiment * w
        total_w += w
        high += 1 if impact >= 2 else 0
        confirmed_high += 1 if impact >= 2 and outlets >= MIN_CONFIRMING_OUTLETS else 0
        articles += len(members)
        lead.sources = outlets  # type: ignore[attr-defined]  # shown as "n bronnen"
        lead.event_sentiment = sentiment  # type: ignore[attr-defined]
        lead.event_impact = impact  # type: ignore[attr-defined]
        scored.append((w, lead))
    scored.sort(key=lambda x: -x[0])
    score = weighted / total_w if total_w > 0 else 0.0
    return MarketSentiment(
        coin,
        score,
        len(scored),
        high,
        [it for _, it in scored[:5]],
        articles=articles,
        confirmed_high_impact=confirmed_high,
    )


def _published(item: NewsItem) -> datetime:
    return item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=UTC)


def _fetched(item: NewsItem) -> datetime:
    """When we first stored the article (its publication date for rows without one)."""
    return aware(item.fetched_at) if item.fetched_at is not None else _published(item)


# After this many failed Claude assessments an item is set aside instead of retried forever.
MAX_ASSESS_ATTEMPTS = 3
FAILED_SUMMARY_NL = "Beoordeling door Claude mislukt; bericht overgeslagen."


class NewsService:
    def __init__(
        self,
        fetcher: NewsFetcher,
        analyst: NewsAnalyst,
        coins: list[str],
        max_age_hours: int,
        batch_size: int,
    ):
        self.fetcher = fetcher
        self.analyst = analyst
        self.coins = coins
        self.max_age_hours = max_age_hours
        self.batch_size = batch_size
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()  # one refresh at a time (scheduler and the button)
        # Set by the app: the last known prices per market (for the event registry's price at
        # first sight), and what runs after a refresh assessed articles (the shock scan).
        self.price_lookup: Callable[[], dict[str, float]] | None = None
        self.after_assess: Callable[[Session, list[NewsItem]], Awaitable[None]] | None = None

    def _next_batch(self, session: Session, since: datetime) -> list[NewsItem]:
        """Fresh items in one batch; items that failed before go one at a time, so one bad
        article cannot keep a whole batch from being scored."""
        batch = pending_items(session, max(self.batch_size, 1), since=since)
        fresh = [it for it in batch if not it.assess_attempts]
        return fresh or batch[:1]

    def _count_failure(self, session: Session, items: list[NewsItem], now: datetime) -> int:
        """Add one failed attempt to ``items``; quarantine those at MAX_ASSESS_ATTEMPTS."""
        quarantined = 0
        for it in items:
            if it.assessed:
                continue
            it.assess_attempts = (it.assess_attempts or 0) + 1
            if it.assess_attempts >= MAX_ASSESS_ATTEMPTS:
                it.assessed, it.assessed_at = True, now
                it.assessment_model = self.analyst.model
                it.relevant, it.sentiment, it.impact = False, 0.0, 0
                it.coins, it.event_key = "", None
                it.summary_nl = FAILED_SUMMARY_NL
                quarantined += 1
                log.warning(
                    "news item %s set aside after %d failed assessments",
                    it.id,
                    MAX_ASSESS_ATTEMPTS,
                )
        session.commit()
        return quarantined

    def _register(self, session: Session, batch: list[NewsItem]) -> None:
        """Bring the event registry up to date for a scored batch (best-effort)."""
        try:
            prices = self.price_lookup() if self.price_lookup is not None else None
            register_items(session, batch, prices=prices, tracked=self.coins)
        except Exception:  # noqa: BLE001 - the registry is context, never block the news
            session.rollback()
            log.exception("event registry update failed")

    async def refresh(self, session: Session, max_batches: int = 4) -> dict[str, int | str | None]:
        """Fetch new items and assess pending ones. Safe to call from the scheduler or UI."""
        async with self._lock:
            return await self._refresh(session, max_batches)

    async def _refresh(self, session: Session, max_batches: int) -> dict[str, int | str | None]:
        errors: list[str] = []
        now = datetime.now(UTC)
        fetched: list[RawItem] = []
        inserted = 0
        try:
            fetched = await self.fetcher.fetch_all()
            inserted = store_items(session, fetched, self.max_age_hours, now)
        except Exception as exc:  # keep going: Fear & Greed and the assessment still run
            session.rollback()
            errors.append(f"nieuws ophalen mislukt: {describe_error(exc)}")
            log.exception("News fetch failed")
        try:
            clamp_future_items(session, now)
            backfill_feed_hosts(session)
            prune_old_news(session, now)
            prune_usage(session, now)
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("News housekeeping failed")
        fetch_mood = getattr(self.fetcher, "fetch_fear_greed", None)
        if fetch_mood is not None:
            try:
                mood = await fetch_mood()
                if mood:
                    save_market_mood(session, mood)
                else:  # switched off or failed: no stale value for Claude
                    clear_market_mood(session)
            except Exception:  # noqa: BLE001
                session.rollback()
                log.exception("Fear & Greed update failed")
        fetch_flows = getattr(self.fetcher, "fetch_etf_flows", None)
        if fetch_flows is not None:
            try:
                flows = await fetch_flows()
                if not flows:  # switched off or no key: nothing stale for Claude
                    clear_etf_flows(session)
                for summary in flows.values():
                    if summary:
                        save_etf_flows(session, summary)
                    # A failed coin keeps its stored value until load_etf_flows finds it old.
            except Exception:  # noqa: BLE001
                session.rollback()
                log.exception("ETF flows update failed")
        assessed = 0
        scored: list[NewsItem] = []
        since = now - timedelta(hours=self.max_age_hours)
        if self.analyst.available:
            for _ in range(max_batches):
                batch = self._next_batch(session, since)
                if not batch:
                    break
                try:
                    results = await self.analyst.assess(
                        session, batch, self.coins, known_keys=recent_event_keys(session)
                    )
                    n = apply_assessments(session, batch, results, self.analyst.model, self.coins)
                except BudgetExceeded as exc:
                    session.rollback()
                    errors.append(str(exc))
                    log.warning("News assessment stopped: %s", exc)
                    break
                except anthropic.APITimeoutError as exc:
                    # Before the connection branch (it is a subclass): a batch that times out
                    # may be one an article forces into a runaway answer. Count it, so it is
                    # split up and set aside after a few tries instead of paid for every run.
                    session.rollback()
                    errors.append(f"Claude-beoordeling mislukt: {describe_error(exc)}")
                    log.warning("News assessment timed out: %s", describe_error(exc))
                    self._count_failure(session, batch, now)
                    break
                except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                    # The API is down or slow: not the articles' fault, try again next run.
                    session.rollback()
                    errors.append(f"Claude-beoordeling mislukt: {describe_error(exc)}")
                    log.warning("News assessment failed: %s", describe_error(exc))
                    break
                except Exception as exc:  # a batch Claude could not score: count it
                    session.rollback()
                    errors.append(f"Claude-beoordeling mislukt: {describe_error(exc)}")
                    log.warning("News assessment failed: %s", describe_error(exc))
                    self._count_failure(session, batch, now)
                    continue
                assessed += n
                scored += [it for it in batch if it.assessed and it.relevant]
                self._register(session, batch)
                skipped = [it for it in batch if not it.assessed]
                if skipped:  # Claude left these out; count it so they cannot loop forever
                    self._count_failure(session, skipped, now)
                if n == 0:
                    break  # Claude returned nothing usable; do not loop forever
        if self.after_assess is not None:
            try:
                await self.after_assess(session, scored)
            except Exception:  # noqa: BLE001 - the news itself is stored; never fail the run
                session.rollback()
                log.exception("after-assessment hook failed")
        # Without a key nothing is assessed. That is a choice, not a failure of this run: it
        # is reported as 'assessing': False (and on the page), never as last_error.
        self.last_error = "; ".join(dict.fromkeys(errors)) or None
        self.last_run = datetime.now(UTC)
        return {
            "fetched": len(fetched),
            "inserted": inserted,
            "assessed": assessed,
            "assessing": self.analyst.available,
            "pending": len(pending_items(session, 1000, since=since)),
            "error": self.last_error,
        }
