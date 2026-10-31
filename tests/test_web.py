from datetime import UTC, datetime

from app.strategy.model import DEFAULT_STRATEGY
from app.web import ago, amount, condition_nl, dt, eur, indicator_nl, nl, pct, price


def test_dutch_number_formats():
    assert eur(1234.5) == "€ 1.234,50"
    assert eur(-3.2) == "-€ 3,20"
    assert eur(None) == "–"
    assert pct(2.345) == "+2,3%"
    assert pct(-0.5) == "-0,5%"
    assert pct(67, 0, signed=False) == "67%"
    assert price(45000.123) == "45.000,12"
    assert price(0.5234) == "0,5234"
    assert amount(0.05) == "0,05"
    assert amount(1.0) == "1"
    assert amount(1234.5) == "1.234,5"


def test_dates_and_relative_time():
    moment = datetime(2026, 10, 31, 11, 5, tzinfo=UTC)
    assert dt(moment) == "31 okt 11:05"
    assert dt(moment, with_year=True) == "31 okt 2026 11:05"
    assert dt(None) == "–"
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert ago(now, now) == "zojuist"
    assert ago(moment, now) == "55 min geleden"
    assert ago(datetime(2026, 10, 31, 8, 0, tzinfo=UTC), now) == "4 uur geleden"
    assert ago(datetime(2026, 10, 30, 8, 0, tzinfo=UTC), now) == "gisteren"
    assert ago(None) == "nog niet"


def test_labels_and_strategy_in_words():
    assert nl("paper") == "Oefenmodus"
    assert nl("buy") == "Kopen"
    assert nl("stop_loss") == "Stop-loss"
    assert nl("iets_onbekends") == "iets_onbekends"
    assert [indicator_nl(i) for i in DEFAULT_STRATEGY.indicators] == ["RSI 14", "EMA 200"]
    assert [condition_nl(c) for c in DEFAULT_STRATEGY.buy] == [
        "rsi14 lager dan 35",
        "koers hoger dan ema200",
    ]
