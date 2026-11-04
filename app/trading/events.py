"""Scheduled macro moments and news shocks: the hours a disciplined trader stays out.

Two sources of "risk-off":

* A **calendar** of known market movers (FOMC rate decisions, US CPI releases, anything Kris
  adds in the settings). Around each one, from ``before_min`` before to ``after_min`` after, the
  bot opens no new positions. Selling and the mechanical stop-loss keep working.
* A **news shock**: a market-wide or coin-specific news fact of the highest impact with strongly
  negative sentiment (an exchange hack, an exchange in trouble), confirmed by a second outlet.
  Such an article is a trigger; app/trading/shock.py decides whether it pauses buying (Claude
  tells a new shock from a repeat) and for how long.

Calendar lines are plain text, one per line, in UTC like everything else in the app:
``2026-12-09 19:00 FOMC-rentebesluit``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})\s+(\S.*)$")

# Decision day of each two-day FOMC meeting (federalreserve.gov/monetarypolicy/fomccalendars.htm);
# the statement comes out at 14:00 Eastern. 2027 dates are tentative until confirmed.
FOMC_DECISION_DAYS = (
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-09",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
)
# US CPI release days (bls.gov/schedule/news_release/cpi.htm), 08:30 Eastern.
CPI_RELEASE_DAYS = (
    "2026-11-10",
    "2026-12-10",
    "2027-01-13",
    "2027-02-11",
    "2027-03-10",
    "2027-04-13",
    "2027-05-12",
    "2027-06-10",
    "2027-07-14",
    "2027-08-11",
    "2027-09-14",
    "2027-10-14",
    "2027-11-10",
    "2027-12-10",
)
FOMC_LABEL = "FOMC-rentebesluit"
CPI_LABEL = "CPI-inflatiecijfer VS"
SHOCK_MIN_IMPACT = 3
SHOCK_MAX_SENTIMENT = -0.5


@dataclass(frozen=True)
class MarketEvent:
    at: datetime  # UTC
    label: str

    def window(self, before_min: int, after_min: int) -> tuple[datetime, datetime]:
        return self.at - timedelta(minutes=before_min), self.at + timedelta(minutes=after_min)

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "label": self.label}


def _eastern(day: str, hhmm: str) -> datetime:
    local = datetime.fromisoformat(f"{day} {hhmm}").replace(tzinfo=NEW_YORK)
    return local.astimezone(UTC)


def default_calendar_lines() -> list[str]:
    """FOMC decisions and CPI releases through 2027 as calendar lines (UTC)."""
    events = [MarketEvent(_eastern(d, "14:00"), FOMC_LABEL) for d in FOMC_DECISION_DAYS]
    events += [MarketEvent(_eastern(d, "08:30"), CPI_LABEL) for d in CPI_RELEASE_DAYS]
    events.sort(key=lambda e: e.at)
    return [format_line(e) for e in events]


def format_line(event: MarketEvent) -> str:
    return f"{event.at:%Y-%m-%d %H:%M} {event.label}"


def parse_line(line: str) -> MarketEvent:
    m = LINE_RE.match(line.strip())
    if not m:
        raise ValueError(
            f"'{line.strip()[:60]}' moet eruitzien als '2026-12-09 19:00 Omschrijving'"
        )
    day, hour, minute, label = m.groups()
    try:
        at = datetime.fromisoformat(f"{day} {int(hour):02d}:{minute}").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"'{line.strip()[:60]}': geen geldige datum of tijd") from exc
    return MarketEvent(at, label.strip())


def parse_calendar(lines: list[str]) -> list[MarketEvent]:
    """Parse calendar lines; a line that does not parse is logged and skipped."""
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(parse_line(line))
        except ValueError as exc:
            log.warning("calendar line skipped: %s", exc)
    return sorted(events, key=lambda e: e.at)


def validate_calendar_lines(raw: Any) -> list[str]:
    """Settings form/API input -> clean, sorted lines. Raises ValueError naming the bad line."""
    parts = raw if isinstance(raw, list) else re.split(r"[\r\n;]+", str(raw))
    events = sorted({parse_line(p) for p in parts if str(p).strip()}, key=lambda e: e.at)
    return [format_line(e) for e in events]


def active_event(
    events: list[MarketEvent], now: datetime, before_min: int, after_min: int
) -> MarketEvent | None:
    """The event whose risk-off window contains ``now``; the nearest one if windows overlap."""
    hits = []
    for e in events:
        start, end = e.window(before_min, after_min)
        if start <= now <= end:
            hits.append(e)
    return min(hits, key=lambda e: abs((e.at - now).total_seconds())) if hits else None


def upcoming_events(
    events: list[MarketEvent], now: datetime, limit: int = 3, horizon_hours: float | None = None
) -> list[MarketEvent]:
    out = [e for e in events if e.at >= now]
    if horizon_hours is not None:
        out = [e for e in out if e.at <= now + timedelta(hours=horizon_hours)]
    return out[:limit]


def confirmed_triggers(session, now: datetime, hours: float, coins: list[str] | None = None):
    """Shock triggers of the last ``hours``, newest first: assessed, relevant articles of the
    highest impact with strongly negative sentiment, for one of ``coins`` or the whole market
    (every coin when ``coins`` is None).

    Like the sentiment, a trigger needs a second outlet: one article (one press release, one
    hostile feed) cannot pause buying for every coin. The other outlets' articles about the
    same event (same event key) count as confirmation whatever impact they were given. Articles
    dated in the future never count (they would keep a pause going for months).
    """
    from sqlalchemy import select

    from app.db import NewsItem
    from app.news.service import MIN_CONFIRMING_OUTLETS, event_key_for, outlet_of

    if hours <= 0:
        return []
    window = (
        NewsItem.assessed.is_(True),
        NewsItem.relevant.is_(True),
        NewsItem.published_at >= now - timedelta(hours=hours),
        NewsItem.published_at <= now,
    )
    rows = session.scalars(
        select(NewsItem)
        .where(
            *window,
            NewsItem.impact >= SHOCK_MIN_IMPACT,
            NewsItem.sentiment <= SHOCK_MAX_SENTIMENT,
        )
        .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
    ).all()
    wanted = set(coins) if coins is not None else None
    candidates = [
        it
        for it in rows
        if wanted is None or "ALL" in it.coin_list() or wanted & set(it.coin_list())
    ]
    if not candidates:
        return []
    outlets: dict[str, set[str]] = {}
    for it in session.scalars(select(NewsItem).where(*window)):
        outlets.setdefault(event_key_for(it), set()).add(outlet_of(it))
    return [
        it for it in candidates if len(outlets.get(event_key_for(it), ())) >= MIN_CONFIRMING_OUTLETS
    ]


def recent_shock(session, coin: str, hours: int, now: datetime | None = None):
    """The newest confirmed shock trigger for ``coin`` (or the whole market) within ``hours``;
    None when there is none. Returns the NewsItem (see confirmed_triggers)."""
    now = now or datetime.now(UTC)
    found = confirmed_triggers(session, now, hours, [coin])
    return found[0] if found else None
