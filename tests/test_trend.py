import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, Setting
from app.trading.trend import (
    load_trend,
    load_trends,
    refresh_trends,
    regime_of,
    save_trend,
    trend_from_candles,
)


def candles(closes, step):
    return [
        [1_600_000_000_000 + i * step, c, c * 1.02, c * 0.98, float(c), 1.0]
        for i, c in enumerate(closes)
    ]


WEEK = 7 * 86_400_000
MONTH = 30 * 86_400_000


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def test_uptrend_above_200wma_and_rsi_above_50():
    # 210 weeks rising slowly, 24 months rising: above the average, monthly RSI well above 50.
    weekly = candles([100 + i for i in range(210)], WEEK)
    monthly = candles([100 + 10 * i for i in range(24)], MONTH)
    t = trend_from_candles("BTC-EUR", weekly, monthly)
    assert t["regime"] == "up" and t["regime_nl"] == "opwaarts"
    assert t["above_wma200"] is True and t["vs_wma200_pct"] > 0
    assert t["monthly_rsi"] > 50 and t["monthly_rsi_above_50"] is True
    assert math.isclose(t["wma200"], sum(range(110, 310)) / 200)
    assert "boven het 200-weeks gemiddelde" in t["summary_nl"]
    assert "lange trend opwaarts" in t["summary_nl"]


def test_downtrend_below_200wma_and_rsi_below_50():
    weekly = candles([400 - i for i in range(210)], WEEK)
    monthly = candles([500 - 10 * i for i in range(24)], MONTH)
    t = trend_from_candles("BTC-EUR", weekly, monthly)
    assert t["regime"] == "down" and t["regime_nl"] == "neerwaarts"
    assert t["above_wma200"] is False and t["monthly_rsi_above_50"] is False
    assert "onder het 200-weeks gemiddelde" in t["summary_nl"]


def test_young_market_is_unknown_or_mixed():
    weekly = candles([1 + 0.01 * i for i in range(30)], WEEK)
    monthly = candles([1 + 0.1 * i for i in range(6)], MONTH)
    t = trend_from_candles("FARTCOIN-EUR", weekly, monthly)
    assert t["regime"] == "unknown" and t["wma200"] is None and t["monthly_rsi"] is None
    assert "te weinig historie" in t["summary_nl"] and "30 van 200 weken" in t["summary_nl"]
    # Enough months but not enough weeks: the one known side decides.
    monthly = candles([1 + 0.1 * i for i in range(24)], MONTH)
    t = trend_from_candles("FARTCOIN-EUR", weekly, monthly)
    assert t["regime"] == "up" and t["above_wma200"] is None


def test_regime_mixed_when_sides_disagree():
    assert regime_of(True, False) == "mixed"
    assert regime_of(False, True) == "mixed"
    assert regime_of(None, None) == "unknown"
    assert regime_of(True, None) == "up"


def test_price_override_decides_side():
    weekly = candles([100] * 210, WEEK)
    monthly = candles([100] * 20, MONTH)
    assert trend_from_candles("X-EUR", weekly, monthly, price=90)["above_wma200"] is False
    assert trend_from_candles("X-EUR", weekly, monthly, price=110)["above_wma200"] is True


class FakeClient:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def candles(self, market, interval, limit):
        self.calls.append((market, interval, limit))
        if market in self.fail:
            raise RuntimeError("market not found")
        n = 210 if interval == "1W" else 30
        return candles([100 + i for i in range(n)], WEEK if interval == "1W" else MONTH)


async def test_refresh_stores_per_market_and_skips_failures(session):
    client = FakeClient(fail={"FARTCOIN-EUR"})
    out = await refresh_trends(client, session, ["BTC-EUR", "FARTCOIN-EUR", "SOL-EUR"])
    assert [t["market"] for t in out] == ["BTC-EUR", "SOL-EUR"]
    assert ("BTC-EUR", "1W", 210) in client.calls and ("BTC-EUR", "1M", 60) in client.calls
    assert load_trend(session, "BTC-EUR")["regime"] == "up"
    assert load_trend(session, "FARTCOIN-EUR") is None
    assert [t["market"] for t in load_trends(session, ["SOL-EUR", "BTC-EUR", "FARTCOIN-EUR"])] == [
        "SOL-EUR",
        "BTC-EUR",
    ]
    # Saving again overwrites instead of duplicating.
    save_trend(session, {"market": "BTC-EUR", "regime": "down"})
    assert load_trend(session, "BTC-EUR")["regime"] == "down"
    assert session.query(Setting).filter(Setting.key.like("trend:%")).count() == 2
