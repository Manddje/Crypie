import json

import pytest
from sqlalchemy.orm import sessionmaker

from app import settings_store as cfg
from app.config import Settings
from app.db import Base, Setting, make_engine


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MAX_TRADE_EUR", "100")
    monkeypatch.setenv("MARKETS", "BTC-EUR,ETH-EUR")
    return Settings(_env_file=None)


def test_environment_is_the_fallback(session, env):
    assert cfg.load_overrides(session) == {}
    s = cfg.effective_settings(session, env)
    assert s.max_trade_eur == 100.0 and s.markets == ["BTC-EUR", "ETH-EUR"]


def test_saved_values_win_and_defaults_are_not_stored(session, env):
    values = cfg.parse_values({"max_trade_eur": "250", "markets": "sol-eur, btc-eur, SOL-EUR"})
    assert values == {"max_trade_eur": 250.0, "markets": ["SOL-EUR", "BTC-EUR"]}
    cfg.save_overrides(session, values, env)
    s = cfg.effective_settings(session, env)
    assert s.max_trade_eur == 250.0 and s.markets == ["SOL-EUR", "BTC-EUR"]
    assert s.coins == ["SOL", "BTC"]
    # Saving the environment value again removes the override instead of storing a copy.
    cfg.save_overrides(session, {"max_trade_eur": 100.0}, env)
    assert set(cfg.load_overrides(session)) == {"markets"}
    assert cfg.reset_overrides(session, ["markets"]) == 1
    assert cfg.load_overrides(session) == {}


def test_parse_rejects_bad_input_with_dutch_messages():
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values(
            {
                "max_trade_eur": "abc",
                "max_trades_per_day": "2.5",
                "max_exposure_pct": "150",
                "markets": "BTC-USD",
                "news_rss_feeds": "ftp://x",
                "claude_model_news": "gpt-5",
                "trading_mode": "demo",
                "bitvavo_api_key": "secret",
            }
        )
    errors = exc.value.errors
    assert "getal" in errors["max_trade_eur"]
    assert "heel getal" in errors["max_trades_per_day"]
    assert "maximaal 100" in errors["max_exposure_pct"]
    assert "BTC-USD" in errors["markets"]
    assert "adres" in errors["news_rss_feeds"]
    assert "claude-" in errors["claude_model_news"]
    assert "keuze" in errors["trading_mode"]
    assert "niet aanpasbare" in errors["bitvavo_api_key"]


def test_parse_accepts_forms_values():
    values = cfg.parse_values(
        {
            "trading_enabled": "false",
            "news_enabled": "true",
            "daily_loss_limit_pct": "2,5",
            "news_rss_feeds": "https://a.example/rss\r\nhttps://b.example/feed\n\n",
            "claude_unavailable_mode": "indicators",
        }
    )
    assert values["trading_enabled"] is False and values["news_enabled"] is True
    assert values["daily_loss_limit_pct"] == 2.5
    assert values["news_rss_feeds"] == ["https://a.example/rss", "https://b.example/feed"]
    assert values["claude_unavailable_mode"] == "indicators"


def test_preview_checks_cross_field_rules(session, env):
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.preview(session, {"min_trade_eur": 500.0}, env)
    assert "minimum" in exc.value.errors["min_trade_eur"]
    candidate = cfg.preview(session, {"min_trade_eur": 50.0, "max_trade_eur": 60.0}, env)
    assert candidate.min_trade_eur == 50.0


def test_unreadable_or_invalid_stored_values_are_ignored(session, env):
    session.add(Setting(key="config:max_trade_eur", value="not json"))
    session.add(Setting(key="config:trading_mode", value=json.dumps("demo")))
    session.add(Setting(key="config:max_trades_per_day", value=json.dumps(9)))
    session.add(Setting(key="config:not_a_setting", value=json.dumps(1)))
    session.commit()
    s = cfg.effective_settings(session, env)
    assert s.max_trade_eur == 100.0 and s.trading_mode == "paper" and s.max_trades_per_day == 9


def test_describe_marks_overrides(session, env):
    cfg.save_overrides(session, {"trade_interval_min": 15}, env)
    groups = cfg.describe(session, cfg.effective_settings(session, env), env)
    assert [g["key"] for g in groups] == ["trading", "limits", "claude", "news"]
    by_key = {f["key"]: f for g in groups for f in g["fields"]}
    assert by_key["trade_interval_min"]["overridden"] is True
    assert by_key["trade_interval_min"]["value"] == 15
    assert by_key["trade_interval_min"]["default"] == 60
    assert by_key["max_trade_eur"]["overridden"] is False
    assert by_key["markets"]["text"] == "BTC-EUR, ETH-EUR"
    assert by_key["trading_mode"]["text"] == "Oefenmodus"
    assert "bitvavo_api_key" not in by_key and "live_trading_ack" not in by_key
