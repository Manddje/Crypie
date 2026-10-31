"""Glue: store fetched items, run assessments, aggregate market sentiment."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.news_analyst import NewsAnalyst, apply_assessments, normalize_key
from app.db import NewsItem, Setting
from app.news.sources import NewsFetcher, RawItem

log = logging.getLogger(__name__)


def store_items(session: Session, raw: list[RawItem], max_age_hours: int) -> int:
    """Insert unseen items (by URL). Returns the number inserted."""
    if not raw:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    fresh = {r.url: r for r in raw if r.published_at >= cutoff}
    if not fresh:
        return 0
    existing = set(session.scalars(select(NewsItem.url).where(NewsItem.url.in_(list(fresh)))).all())
    inserted = 0
    for url, r in fresh.items():
        if url in existing:
            continue
        session.add(
            NewsItem(
                source=r.source,
                source_name=r.source_name,
                url=url,
                title=r.title,
                summary=r.summary,
                published_at=r.published_at,
                source_coins=",".join(r.coins) or None,
                source_votes=json.dumps(r.votes) if r.votes else None,
            )
        )
        inserted += 1
    session.commit()
    return inserted


MARKET_MOOD_KEY = "market_mood"


def save_market_mood(session: Session, mood: dict) -> None:
    row = session.get(Setting, MARKET_MOOD_KEY)
    payload = json.dumps(mood)
    if row is None:
        session.add(Setting(key=MARKET_MOOD_KEY, value=payload))
    else:
        row.value = payload
    session.commit()


def load_market_mood(session: Session) -> dict | None:
    row = session.get(Setting, MARKET_MOOD_KEY)
    return json.loads(row.value) if row else None


def pending_items(session: Session, limit: int) -> list[NewsItem]:
    return list(
        session.scalars(
            select(NewsItem)
            .where(NewsItem.assessed.is_(False))
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
        )
    )


def recent_items(session: Session, hours: int = 24, limit: int = 100) -> list[NewsItem]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return list(
        session.scalars(
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff)
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
        )
    )


def event_key_for(item: NewsItem) -> str:
    """Claude's event key, or the normalised title for items assessed before keys existed."""
    return item.event_key or ("title:" + normalize_key(item.title))


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


def recent_event_keys(
    session: Session, hours: int = 48, limit: int = 40, now: datetime | None = None
) -> list[str]:
    """Most-used event keys of the last ``hours``, so Claude reuses them for new items."""
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=hours)
    rows = session.execute(
        select(NewsItem.event_key, func.count(NewsItem.id))
        .where(NewsItem.event_key.is_not(None), NewsItem.published_at >= cutoff)
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
            "top": [
                {
                    "title": t.title,
                    "sources": getattr(t, "sources", 1),
                    "summary_nl": t.summary_nl,
                    "sentiment": t.sentiment,
                    "impact": t.impact,
                    "url": t.url,
                    "published_at": t.published_at,
                }
                for t in self.top
            ],
        }


HORIZON_HALF_LIFE_H = {"hours": 6.0, "days": 36.0, "weeks": 168.0}


def market_sentiment(
    session: Session, coin: str, hours: int = 48, now: datetime | None = None
) -> MarketSentiment:
    """Impact-weighted, time-decayed average sentiment of relevant news for one coin.

    Articles about the same news fact (same event key) are folded into one event first, so a
    hack that twelve outlets report weighs like one hack, not twelve. The event takes the
    highest impact and the average sentiment of its articles, and ages from its newest article.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    rows = session.scalars(
        select(NewsItem).where(
            NewsItem.assessed.is_(True),
            NewsItem.relevant.is_(True),
            NewsItem.published_at >= cutoff,
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
    articles = 0
    scored: list[tuple[float, NewsItem]] = []
    for members in events.values():
        lead = max(members, key=lambda m: (m.impact or 0, _published(m)))
        impact = lead.impact or 0
        sentiment = sum(m.sentiment or 0.0 for m in members) / len(members)
        age_h = max((now - max(_published(m) for m in members)).total_seconds() / 3600, 0.0)
        half_life = HORIZON_HALF_LIFE_H.get(lead.horizon or "days", 36.0)
        decay = math.exp(-math.log(2) * age_h / half_life)
        w = impact * decay
        weighted += sentiment * w
        total_w += w
        high += 1 if impact >= 2 else 0
        articles += len(members)
        lead.sources = len(members)  # type: ignore[attr-defined]  # shown as "n bronnen"
        scored.append((w, lead))
    scored.sort(key=lambda x: -x[0])
    score = weighted / total_w if total_w > 0 else 0.0
    return MarketSentiment(
        coin, score, len(events), high, [it for _, it in scored[:5]], articles=articles
    )


def _published(item: NewsItem) -> datetime:
    return item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=UTC)


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

    async def refresh(self, session: Session, max_batches: int = 4) -> dict[str, int | str | None]:
        """Fetch new items and assess pending ones. Safe to call from the scheduler or UI."""
        fetched = await self.fetcher.fetch_all()
        inserted = store_items(session, fetched, self.max_age_hours)
        fetch_mood = getattr(self.fetcher, "fetch_fear_greed", None)
        mood = await fetch_mood() if fetch_mood else None
        if mood:
            save_market_mood(session, mood)
        assessed = 0
        self.last_error = None
        if self.analyst.available:
            for _ in range(max_batches):
                batch = pending_items(session, self.batch_size)
                if not batch:
                    break
                try:
                    results = await self.analyst.assess(
                        session, batch, self.coins, known_keys=recent_event_keys(session)
                    )
                except BudgetExceeded as exc:
                    self.last_error = str(exc)
                    log.warning("News assessment stopped: %s", exc)
                    break
                except Exception as exc:  # keep the scheduler alive
                    self.last_error = f"Claude-beoordeling mislukt: {exc}"
                    log.exception("News assessment failed")
                    break
                n = apply_assessments(session, batch, results, self.analyst.model)
                assessed += n
                if n == 0:
                    break  # Claude returned nothing usable; do not loop forever
        else:
            self.last_error = "ANTHROPIC_API_KEY ontbreekt; nieuws wordt niet beoordeeld"
        self.last_run = datetime.now(UTC)
        return {
            "fetched": len(fetched),
            "inserted": inserted,
            "assessed": assessed,
            "pending": len(pending_items(session, 1000)),
            "error": self.last_error,
        }
