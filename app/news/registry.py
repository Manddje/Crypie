"""The event registry: one row per news fact (event key) across all its articles.

The news assessment gives every relevant article an event key; articles about the same fact
share it. The registry remembers per key when the fact first and last appeared, how heavy it
was, which coins it touched, the price when it first appeared and the shock pauses it caused.
The shock assessment (app/trading/shock.py) reads it to tell a new shock from a repeat.

Aggregates are recomputed from the stored articles on every update, so a re-assessed article
is never counted twice. The registry outlives the news retention: a count or first sighting is
never lowered because old articles were pruned.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import NewsEvent, NewsItem, clip
from app.news.sources import aware

log = logging.getLogger(__name__)

MARKET_WIDE = "ALL"
MAX_COINS = 10  # 10 x (ticker + comma) fits coins VARCHAR(128)
# The current price counts as "the price when the fact first appeared" only this soon after.
FRESH_PRICE_WINDOW = timedelta(hours=2)
SUMMARY_MAX = 400
KEY_MAX = 64


def merge_coins(*groups: Iterable[str]) -> str:
    """The union of coin lists as stored: "ALL" absorbs everything, else sorted tickers."""
    coins: set[str] = set()
    for group in groups:
        coins.update(c for c in group if c)
    if MARKET_WIDE in coins:
        return MARKET_WIDE
    return ",".join(sorted(coins)[:MAX_COINS])


def members_of(session: Session, key: str) -> list[NewsItem]:
    """Every stored, relevant article with this event key."""
    return list(
        session.scalars(
            select(NewsItem).where(
                NewsItem.event_key == key,
                NewsItem.assessed.is_(True),
                NewsItem.relevant.is_(True),
            )
        )
    )


def upsert_event(
    session: Session,
    key: str,
    members: list[NewsItem],
    *,
    prices: dict[str, float] | None = None,
    tracked: list[str] | None = None,
    now: datetime | None = None,
) -> NewsEvent | None:
    """Create or update the registry row for ``key`` from its articles (no commit).

    ``prices`` (market -> price, e.g. {"BTC-EUR": 69515.0}) fills the price at first sight for
    coins that do not have one yet, but only while the fact is at most FRESH_PRICE_WINDOW old.
    """
    key = (key or "")[:KEY_MAX]
    event = session.get(NewsEvent, key) if key else None
    if not key or (event is None and not members):
        return event
    now = now or datetime.now(UTC)
    ordered = sorted(members, key=lambda m: (aware(m.published_at), m.id or 0))
    if event is None:
        first = aware(ordered[0].published_at)
        event = NewsEvent(event_key=key, first_seen_at=first, last_seen_at=first)
        event.article_count = 0
        event.shock_count = 0
        event.last_trigger_id = 0
        event.coins = ""
        session.add(event)
    if ordered:
        first = aware(ordered[0].published_at)
        last = aware(ordered[-1].published_at)
        event.first_seen_at = min(aware(event.first_seen_at), first)
        event.last_seen_at = max(aware(event.last_seen_at), last)
        event.article_count = max(event.article_count or 0, len(ordered))
        impacts = [m.impact for m in ordered if m.impact is not None]
        if event.max_impact is not None:
            impacts.append(event.max_impact)
        if impacts:
            event.max_impact = max(impacts)
        sentiments = [m.sentiment for m in ordered if m.sentiment is not None]
        if event.min_sentiment is not None:
            sentiments.append(event.min_sentiment)
        if sentiments:
            event.min_sentiment = min(sentiments)
        event.coins = merge_coins(event.coin_list(), *(m.coin_list() for m in ordered))
        if not event.first_summary_nl:
            lead = next((m for m in ordered if m.summary_nl), None)
            if lead is not None:
                event.first_summary_nl = clip(lead.summary_nl, SUMMARY_MAX)
    _fill_prices(event, prices, tracked, now)
    return event


def _fill_prices(
    event: NewsEvent,
    prices: dict[str, float] | None,
    tracked: list[str] | None,
    now: datetime,
) -> None:
    if not prices or now - aware(event.first_seen_at) > FRESH_PRICE_WINDOW:
        return
    coins = event.coin_list()
    if MARKET_WIDE in coins:
        coins = list(dict.fromkeys([*(tracked or []), "BTC"]))
    stored = event.prices()
    changed = False
    for coin in coins:
        price = prices.get(f"{coin}-EUR")
        if coin not in stored and price and 0 < price < float("inf"):
            stored[coin] = float(price)
            changed = True
    if changed:
        event.price_at_first_seen = json.dumps(stored, sort_keys=True)


def register_items(
    session: Session,
    items: Iterable[NewsItem],
    *,
    prices: dict[str, float] | None = None,
    tracked: list[str] | None = None,
    now: datetime | None = None,
) -> list[NewsEvent]:
    """Bring the registry up to date for the event keys of freshly assessed ``items``."""
    keys = sorted({it.event_key for it in items if it.assessed and it.relevant and it.event_key})
    events = []
    for key in keys:
        event = upsert_event(
            session, key, members_of(session, key), prices=prices, tracked=tracked, now=now
        )
        if event is not None:
            events.append(event)
    session.commit()
    return events


def backfill_events(session: Session) -> int:
    """Registry rows for every event key in the stored news that has none yet (migration).

    Groups the relevant articles per event key; the first sighting is the oldest publication
    date. Idempotent: keys that already have a row are left alone. Returns the rows created.
    """
    known = set(session.scalars(select(NewsEvent.event_key)))
    keys = session.scalars(
        select(NewsItem.event_key)
        .where(
            NewsItem.event_key.is_not(None),
            NewsItem.assessed.is_(True),
            NewsItem.relevant.is_(True),
        )
        .distinct()
    ).all()
    created = 0
    for key in sorted(k for k in keys if k and k[:KEY_MAX] not in known):
        if upsert_event(session, key, members_of(session, key)) is not None:
            created += 1
    session.commit()
    return created


# Words that say nothing about which fact a key names: coins and generic market words.
_GENERIC = frozenset(
    {
        "all",
        "and",
        "the",
        "crypto",
        "cryptocurrency",
        "market",
        "markets",
        "price",
        "prices",
        "news",
        "coin",
        "coins",
        "token",
        "tokens",
        "bitcoin",
        "ethereum",
        "solana",
        "btc",
        "eth",
        "sol",
        "xrp",
        "usd",
        "eur",
    }
)
_WORD = re.compile(r"[a-z0-9]+")


def key_words(key: str, ignore: Iterable[str] = ()) -> set[str]:
    """The meaningful words of an event key ("binance-hack-compensation" -> binance, hack,
    compensation): at least three characters, no coin or generic market words."""
    skip = _GENERIC | {w.lower() for w in ignore}
    return {w for w in _WORD.findall((key or "").lower()) if len(w) >= 3 and w not in skip}


def related_events(
    session: Session,
    key: str,
    now: datetime,
    *,
    days: int = 7,
    limit: int = 8,
    ignore: Iterable[str] = (),
) -> list[NewsEvent]:
    """Other event keys seen in the last ``days`` that share a meaningful word with ``key``
    (e.g. "binance-hack" and "binance-withdrawals-halted"), most recent first."""
    words = key_words(key, ignore)
    if not words:
        return []
    rows = session.scalars(
        select(NewsEvent)
        .where(
            NewsEvent.event_key != key,
            NewsEvent.last_seen_at >= now - timedelta(days=days),
            NewsEvent.first_seen_at <= now,
        )
        .order_by(NewsEvent.last_seen_at.desc())
    ).all()
    return [e for e in rows if key_words(e.event_key, ignore) & words][:limit]
