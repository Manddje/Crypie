from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, NewsItem
from app.trading.events import (
    MarketEvent,
    active_event,
    default_calendar_lines,
    parse_calendar,
    parse_line,
    recent_shock,
    upcoming_events,
    validate_calendar_lines,
)


def test_default_calendar_converts_eastern_to_utc():
    lines = default_calendar_lines()
    assert "2026-12-09 19:00 FOMC-rentebesluit" in lines  # 14:00 EST
    assert "2026-06-17 18:00 FOMC-rentebesluit" in lines  # 14:00 EDT
    assert "2026-11-10 13:30 CPI-inflatiecijfer VS" in lines  # 08:30 EST
    assert "2027-06-10 12:30 CPI-inflatiecijfer VS" in lines  # 08:30 EDT
    assert lines == sorted(lines) and len(lines) == 30


def test_parse_and_validate_lines():
    e = parse_line("2026-12-09 19:00  FOMC-rentebesluit ")
    assert e == MarketEvent(datetime(2026, 12, 9, 19, 0, tzinfo=UTC), "FOMC-rentebesluit")
    assert parse_line("2026-12-09T9:05 X").at.hour == 9
    with pytest.raises(ValueError, match="moet eruitzien als"):
        parse_line("morgen om 3 uur")
    with pytest.raises(ValueError, match="geen geldige datum"):
        parse_line("2026-13-40 19:00 X")
    # Form input: newlines or semicolons, duplicates dropped, sorted, bad lines skipped on parse.
    assert validate_calendar_lines(
        "2026-12-10 13:30 CPI;2026-12-09 19:00 FOMC\n2026-12-09 19:00 FOMC"
    ) == [
        "2026-12-09 19:00 FOMC",
        "2026-12-10 13:30 CPI",
    ]
    assert [e.label for e in parse_calendar(["kapot", "", "2026-12-09 19:00 FOMC"])] == ["FOMC"]


def test_active_and_upcoming_windows():
    fomc = MarketEvent(datetime(2026, 12, 9, 19, 0, tzinfo=UTC), "FOMC")
    cpi = MarketEvent(datetime(2026, 12, 10, 13, 30, tzinfo=UTC), "CPI")
    events = [fomc, cpi]
    assert active_event(events, datetime(2026, 12, 9, 17, 59, tzinfo=UTC), 60, 120) is None
    assert active_event(events, datetime(2026, 12, 9, 18, 0, tzinfo=UTC), 60, 120) == fomc
    assert active_event(events, datetime(2026, 12, 9, 21, 0, tzinfo=UTC), 60, 120) == fomc
    assert active_event(events, datetime(2026, 12, 9, 21, 1, tzinfo=UTC), 60, 120) is None
    assert active_event(events, datetime(2026, 12, 10, 13, 0, tzinfo=UTC), 0, 0) is None
    now = datetime(2026, 12, 9, 20, 0, tzinfo=UTC)
    assert upcoming_events(events, now) == [cpi]
    assert upcoming_events(events, now, horizon_hours=10) == []
    assert upcoming_events(events, datetime(2026, 1, 1, tzinfo=UTC), limit=1) == [fomc]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _news(session, title, sentiment, impact, coins, hours_ago, now, outlet="x.com"):
    it = NewsItem(
        source="rss",
        source_name=outlet,
        feed_host=outlet,
        url=f"https://{outlet}/{title}",
        title=title,
        published_at=now - timedelta(hours=hours_ago),
        assessed=True,
        relevant=True,
        sentiment=sentiment,
        impact=impact,
        coins=coins,
    )
    session.add(it)
    session.commit()
    return it


def test_recent_shock_needs_top_impact_and_strong_negative(session):
    now = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)
    _news(session, "mild", -0.4, 3, "ALL", 1, now)  # not negative enough
    _news(session, "medium", -0.9, 2, "ALL", 1, now)  # impact too low
    _news(session, "old", -0.9, 3, "ALL", 9, now)  # outside the window
    _news(session, "sol-only", -0.9, 3, "SOL", 2, now)
    _news(session, "sol-only", -0.7, 2, "SOL", 2, now, outlet="y.org")  # second outlet
    assert recent_shock(session, "BTC", 6, now) is None
    assert recent_shock(session, "SOL", 6, now).title == "sol-only"
    _news(session, "hack", -0.8, 3, "ALL", 1, now)
    # One outlet is not enough to pause every coin (F37).
    assert recent_shock(session, "BTC", 6, now) is None
    _news(session, "hack", -0.9, 3, "ALL", 1, now, outlet="news.x.com")  # same outlet
    assert recent_shock(session, "BTC", 6, now) is None
    _news(session, "hack", -0.9, 3, "ALL", 1, now, outlet="z.net")
    assert recent_shock(session, "BTC", 6, now).title == "hack"
    assert recent_shock(session, "SOL", 6, now).title == "hack"  # newest shock wins
    assert recent_shock(session, "BTC", 0, now) is None  # pause switched off
