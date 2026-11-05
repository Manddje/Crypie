"""Security fixes in settings, strategy, journal and the pages (audit package "ui").

F21 (ceilings, feed URLs), F28/CSP (no inline scripts), F29/F30 (NaN), F35 (protective exit,
diff, MARKETS on apply), F48 (candles path), F64 (lengths), F65 (query bounds), F66 (list
bounds), F76 (trivial buy conditions), F77 (fail-safe strategy load), F78 (sell first with a
position), F79 (warm-up cap), F80 (flat RSI).
"""

import json
import re

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app import settings_store as cfg
from app.config import Settings
from app.db import Base, Evaluation, Position, SessionLocal, Setting, init_db, make_engine
from app.indicators.core import rsi
from app.main import app
from app.strategy.describe import strategy_diff
from app.strategy.engine import backtest, compute_series, signal_at
from app.strategy.model import (
    DEFAULT_STRATEGY,
    MAX_WARMUP,
    Condition,
    Strategy,
    check_for_use,
    use_problems,
)
from app.strategy.store import STRATEGY_KEY, load_strategy, load_strategy_checked, save_strategy
from app.trading.journal import TAGS_COLUMN_MAX, normalize_tags, save_note
from tests.test_trading import FakeDecider, candles, make_engine_

BASE = "https://api.bitvavo.com/v2"


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("MARKETS", "BTC-EUR,ETH-EUR")
    return Settings(_env_file=None)


def _app_db_cleanup():
    with SessionLocal() as s:
        s.execute(delete(Evaluation))
        s.execute(delete(Setting).where(Setting.key == STRATEGY_KEY))
        s.execute(delete(Setting).where(Setting.key.like(f"{cfg.CONFIG_PREFIX}%")))
        s.commit()


@pytest.fixture
async def api():
    # Shared app database: start from (and leave behind) a clean strategy and settings.
    init_db()
    _app_db_cleanup()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        _app_db_cleanup()


def strategy(**overrides) -> Strategy:
    return Strategy.model_validate(DEFAULT_STRATEGY.model_dump() | overrides)


# -- F29/F30: NaN and infinity in settings -------------------------------------------------


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "1e999"])
@pytest.mark.parametrize("key", ["max_trade_eur", "max_exposure_pct", "max_trades_per_day"])
def test_settings_reject_nan_and_infinity(key, raw):
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({key: raw})
    assert exc.value.errors[key] == "geen geldig getal"


def test_settings_reject_json_nan_number():
    with pytest.raises(cfg.SettingsError):
        cfg.parse_values({"claude_monthly_budget_usd": float("nan")})


def test_stored_nan_is_dropped_on_load(session, env):
    session.add(Setting(key="config:max_trade_eur", value="NaN"))
    session.commit()
    s = cfg.effective_settings(session, env)
    assert s.max_trade_eur == env.max_trade_eur == 100.0


# -- F21: environment-only ceilings and feed addresses ----------------------------------


def test_limits_above_the_environment_ceiling_are_refused(session, monkeypatch):
    monkeypatch.setenv("MAX_TRADE_EUR_CEILING", "200")
    monkeypatch.setenv("MAX_TRADES_PER_DAY_CEILING", "10")
    env = Settings(_env_file=None)
    values = cfg.parse_values({"max_trade_eur": "250", "max_trades_per_day": "11"})
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.preview(session, values, env)
    assert "MAX_TRADE_EUR_CEILING" in exc.value.errors["max_trade_eur"]
    assert "maximaal 10" in exc.value.errors["max_trades_per_day"]
    # At the ceiling is fine.
    ok = cfg.preview(session, cfg.parse_values({"max_trade_eur": "200"}), env)
    assert ok.max_trade_eur == 200
    # The ceiling itself is not editable in the portal.
    with pytest.raises(cfg.SettingsError):
        cfg.parse_values({"max_trade_eur_ceiling": "5000"})
    # The settings page offers the ceiling as the maximum.
    fields = {f["key"]: f for g in cfg.describe(session, env, env) for f in g["fields"]}
    assert fields["max_trade_eur"]["max"] == 200 and fields["max_trade_eur"]["ceiling"] == 200


def test_default_ceilings(env):
    assert env.max_trade_eur_ceiling == 1000
    assert env.max_exposure_pct_ceiling == 100
    assert env.max_trades_per_day_ceiling == 50
    with pytest.raises(cfg.SettingsError):
        cfg.preview(None, {"max_trade_eur": 1500.0}, env)


def test_stored_value_above_a_lowered_ceiling_is_clamped(session, monkeypatch):
    session.add(Setting(key="config:max_trade_eur", value="900"))
    session.add(Setting(key="config:max_exposure_pct", value="90"))
    session.commit()
    monkeypatch.setenv("MAX_TRADE_EUR_CEILING", "250")
    monkeypatch.setenv("MAX_EXPOSURE_PCT_CEILING", "60")
    env = Settings(_env_file=None)
    s = cfg.effective_settings(session, env)
    assert s.max_trade_eur == 250 and s.max_exposure_pct == 60


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/feed",
        "http://[::1]/feed",
        "http://localhost:8000/feed",
        "https://foo.localhost/rss",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://intranet/feed",
        "https://user:secret@example.com/rss",
        "ftp://example.com/feed",
        "file:///etc/passwd",
        "https://example.com:99999/rss",
        "https://example.com/" + "a" * 600,
    ],
)
def test_feed_addresses_must_be_public_http_hostnames(url):
    with pytest.raises(cfg.SettingsError):
        cfg.parse_values({"news_rss_feeds": url})


def test_valid_feeds_still_accepted():
    values = cfg.parse_values(
        {
            "news_rss_feeds": "https://www.coindesk.com/arc/outboundfeeds/rss/\n"
            "https://example.org/feed"
        }
    )
    assert len(values["news_rss_feeds"]) == 2


def test_plain_http_feeds_are_refused():
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({"news_rss_feeds": "http://example.org/feed"})
    assert "gebruik https://" in exc.value.errors["news_rss_feeds"]


# -- F66: list bounds ---------------------------------------------------------------------


def test_list_settings_have_upper_bounds():
    markets = ",".join(f"C{i:02d}-EUR" for i in range(cfg.MAX_MARKETS + 1))
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({"markets": markets})
    assert "maximaal 20" in exc.value.errors["markets"]
    assert len(cfg.parse_values({"markets": markets.rsplit(",", 1)[0]})["markets"]) == 20

    feeds = "\n".join(f"https://feed{i}.example.com/rss" for i in range(cfg.MAX_FEEDS + 1))
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({"news_rss_feeds": feeds})
    assert "maximaal 30" in exc.value.errors["news_rss_feeds"]

    lines = "\n".join(
        f"2030-01-{d:02d} {h:02d}:00 moment" for d in range(1, 29) for h in range(8)
    )  # 224 lines
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({"event_calendar": lines})
    assert "maximaal 200" in exc.value.errors["event_calendar"]
    # The default calendar fits.
    assert cfg.parse_values({"event_calendar": Settings(_env_file=None).event_calendar})

    with pytest.raises(cfg.SettingsError):
        cfg.parse_values({"gdelt_query": "x" * 30_000})


# -- F77 / F79 / F64: strategy model ---------------------------------------------------------


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400", "1e13"])
def test_thresholds_must_be_finite(value):
    payload = DEFAULT_STRATEGY.model_dump_json().replace('"right":35.0', f'"right":{value}')
    assert payload != DEFAULT_STRATEGY.model_dump_json()
    with pytest.raises(ValueError):
        Strategy.model_validate_json(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1e13])
def test_condition_threshold_must_be_finite(value):
    with pytest.raises(ValueError):
        Condition(left="rsi14", op="<", right=value)
    with pytest.raises(ValueError):
        Condition(left="rsi14", op="rising", right=value)


def test_indicator_periods_fit_the_candle_window():
    with pytest.raises(ValueError):
        strategy(indicators=[{"type": "drawdown", "id": "dd", "period": 1500}])
    # Warm-up of indicators plus look-back is capped too.
    with pytest.raises(ValueError, match="warm-up"):
        strategy(
            indicators=[{"type": "sma", "id": "slow", "period": MAX_WARMUP}],
            buy=[{"left": "slow", "op": "rising", "right": 5}],
            sell=[{"left": "slow", "op": "falling", "right": 1}],
        )
    s = strategy(
        indicators=[{"type": "sma", "id": "slow", "period": 800}],
        buy=[{"left": "close", "op": "crosses_above", "right": "slow"}],
        sell=[{"left": "close", "op": "crosses_below", "right": "slow"}],
    )
    assert s.warmup() == 801 and s.candles_needed() == 851
    # The default strategy keeps fetching the same 300 candles as before.
    assert DEFAULT_STRATEGY.candles_needed() == 300


def test_description_is_bounded():
    with pytest.raises(ValueError):
        strategy(description="x" * 4001)
    assert strategy(description="x" * 4000)


# -- F35 / F76: checks before a strategy becomes active ---------------------------------------


def test_protective_exit_is_required_for_use():
    assert use_problems(DEFAULT_STRATEGY) == []
    no_stop = strategy(stop_loss_pct=None, trailing_stop_pct=None, trailing_start_pct=None)
    assert any("geen stop vanaf de instap" in p for p in use_problems(no_stop))
    with pytest.raises(ValueError, match="Strategie niet toegepast"):
        check_for_use(no_stop)
    # A trailing stop that only starts after a rise leaves a new position without a stop.
    assert any("geen stop vanaf de instap" in p for p in use_problems(strategy(stop_loss_pct=None)))
    late = strategy(stop_loss_pct=None, trailing_start_pct=400)
    assert any("geen stop vanaf de instap" in p for p in use_problems(late))
    # A trailing stop from the entry is enough, and so is a stop-loss alone.
    assert use_problems(strategy(stop_loss_pct=None, trailing_start_pct=None)) == []
    assert use_problems(strategy(trailing_stop_pct=None)) == []


@pytest.mark.parametrize(
    "cond",
    [
        {"left": "close", "op": ">", "right": 0},
        {"left": "close", "op": ">=", "right": -5},
        {"left": "volume", "op": ">=", "right": 0},
        {"left": "rsi14", "op": "<=", "right": 100},
        {"left": "rsi14", "op": "<", "right": 101},
        {"left": "rsi14", "op": ">=", "right": 0},
        {"left": "dd7d", "op": ">=", "right": 0},
        {"left": "high", "op": ">=", "right": "low"},
        {"left": "low", "op": "<=", "right": "close"},
        {"left": "ema200", "op": ">", "right": 0},
        {"left": "close", "op": ">=", "right": "close"},
        {"left": "rsi14", "op": "crosses_above", "right": "rsi14"},
    ],
)
def test_trivially_true_hard_buy_conditions_are_refused(cond):
    s = strategy(buy=[cond])
    assert any("altijd waar" in p for p in use_problems(s)), cond
    # As a soft filter next to a real hard condition it is harmless.
    s = strategy(buy=[{"left": "rsi14", "op": "<", "right": 30}, cond | {"soft": True}])
    assert use_problems(s) == []


@pytest.mark.parametrize(
    "cond",
    [
        {"left": "volume", "op": ">", "right": 0},
        {"left": "rsi14", "op": "<", "right": 30},
        {"left": "close", "op": ">", "right": "ema200"},
        {"left": "rsi14", "op": "rising", "right": 1},
        {"left": "close", "op": "<", "right": 0},  # never true: useless, but not a buy flood
    ],
)
def test_real_buy_conditions_pass(cond):
    assert use_problems(strategy(buy=[cond])) == []


def test_strategy_diff_lists_changed_fields():
    new = strategy(stop_loss_pct=None, name="Zonder stop")
    diff = {d["field"]: d for d in strategy_diff(DEFAULT_STRATEGY, new)}
    assert set(diff) == {"name", "stop_loss_pct"}
    assert diff["stop_loss_pct"]["old"] == "5" and diff["stop_loss_pct"]["new"] == "geen"
    assert strategy_diff(DEFAULT_STRATEGY, DEFAULT_STRATEGY) == []


# -- F77: fail-safe load ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        DEFAULT_STRATEGY.model_dump_json().replace('"right":35.0', '"right":null'),
        "{not json",
        '"just a string"',
        json.dumps(DEFAULT_STRATEGY.model_dump() | {"market": "../x"}),
    ],
)
def test_invalid_stored_strategy_falls_back_to_default(session, stored, caplog):
    session.add(Setting(key=STRATEGY_KEY, value=stored))
    session.commit()
    s, warning = load_strategy_checked(session)
    assert s == DEFAULT_STRATEGY and "standaardstrategie" in warning
    assert load_strategy(session) == DEFAULT_STRATEGY
    assert "stored strategy is invalid" in caplog.text


def test_long_stored_description_is_shortened_not_dropped(session):
    data = DEFAULT_STRATEGY.model_dump() | {"name": "Oud", "description": "y" * 9000}
    session.add(Setting(key=STRATEGY_KEY, value=json.dumps(data)))
    session.commit()
    s, warning = load_strategy_checked(session)
    assert warning is None and s.name == "Oud" and len(s.description) == 4000


def test_strategy_without_stop_still_loads(session):
    """Stored before the use checks existed: keeps working, the trading cycle relies on it."""
    old = strategy(name="Oud zonder stop", stop_loss_pct=None, trailing_stop_pct=None)
    save_strategy(session, old)
    assert load_strategy_checked(session) == (old, None)


# -- F78: sell first with an open position ---------------------------------------------------


def _always_both() -> Strategy:
    return strategy(
        indicators=[{"type": "sma", "id": "s2", "period": 2}],
        buy=[{"left": "close", "op": ">", "right": 5}],
        sell=[{"left": "close", "op": "<", "right": 100}],
    )


def test_signal_with_position_checks_sell_first():
    s = _always_both()
    series = compute_series(s, candles([10, 11, 12, 13]))
    assert signal_at(s, series, 3) == "buy"
    assert signal_at(s, series, 3, has_position=True) == "sell"


def test_backtest_exits_on_sell_signal_even_when_buy_also_holds():
    s = _always_both().model_copy(update={"stop_loss_pct": None, "trailing_stop_pct": None})
    result = backtest(s, candles([10, 11, 12, 13, 14]), 1000, 0)
    # Buy on bar 0, sell on bar 1, buy on bar 2, sell on bar 3, buy on bar 4.
    assert [t.reason for t in result.closed_trades] == ["signal", "signal"]


async def test_live_cycle_with_position_sees_the_sell_signal(session):
    engine = make_engine_(
        session,
        {"BTC-EUR": candles([10, 11, 12, 13])},
        {"BTC-EUR": 13.0},
        FakeDecider(available=False),
        strategy=_always_both(),
    )
    session.add(Position(mode="paper", market="BTC-EUR", amount=1, entry_price=10, entry_quote=10))
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.signal == "sell"


# -- F76: live buy-signal frequency in the backtest -------------------------------------------


def test_backtest_reports_live_buy_signal_frequency():
    s = strategy(
        indicators=[{"type": "rsi", "id": "rsi14", "period": 14}],
        buy=[
            {"left": "volume", "op": ">", "right": 0},
            {"left": "rsi14", "op": "<", "right": 5, "soft": True},
        ],
        sell=[{"left": "rsi14", "op": ">", "right": 70}],
    )
    bars = candles([100 + (i % 5) for i in range(100)])
    summary = backtest(s, bars, 1000, 0).summary()
    assert summary["warmup_candles"] == 15
    assert summary["live_buy_signal_pct"] == 100.0
    # Like live: the hard condition alone buys, at half size because the soft filter fails.
    assert summary["trades"] + summary["open_position"] >= 1
    assert summary["half_size_trades"] >= 1
    strict = backtest(s, bars, 1000, 0, soft_mode="strict").summary()
    assert strict["trades"] == 0 and not strict["open_position"]  # the strict version never buys


# -- F80: RSI on flat candles ---------------------------------------------------------------


def test_rsi_is_neutral_without_movement_and_ignores_ticks():
    assert rsi([100.0] * 20, 14)[-1] == 50.0
    # One rounding tick after a flat stretch does not swing the RSI to 0 or 100.
    assert rsi([100.0] * 20 + [100.005], 14)[-1] == 50.0
    assert rsi([100.0] * 20 + [99.995], 14)[-1] == 50.0
    # A real move still counts.
    assert rsi([100.0] * 20 + [101.0], 14)[-1] == 100.0
    assert rsi([float(i) for i in range(1, 20)], 14)[-1] == 100.0


# -- F64: journal tags fit their column -------------------------------------------------------


def test_tags_are_bounded(session):
    tags = normalize_tags(", ".join("t" * 40 + str(i) for i in range(10)))
    assert len(tags) <= TAGS_COLUMN_MAX and all(len(t) <= 32 for t in tags.split(","))
    session.add(Position(mode="paper", market="BTC-EUR", amount=1, entry_price=1, entry_quote=1))
    session.commit()
    with pytest.raises(ValueError, match="tags"):
        save_note(session, 1, "ok", "x" * 300)


# -- API and pages ----------------------------------------------------------------------------


def _no_stop_json() -> dict:
    return json.loads(
        strategy(name="Zonder stop", stop_loss_pct=None, trailing_stop_pct=None).model_dump_json()
    )


async def test_api_and_form_refuse_a_strategy_without_stop(api):
    r = await api.put("/api/strategy", json=_no_stop_json())
    assert r.status_code == 422 and "geen stop vanaf de instap" in r.json()["detail"]
    trivial = DEFAULT_STRATEGY.model_dump() | {"buy": [{"left": "close", "op": ">", "right": 0}]}
    r = await api.put("/api/strategy", json=trivial)
    assert r.status_code == 422 and "altijd waar" in r.json()["detail"]
    assert r.json()["code"] == "invalid_strategy"

    r = await api.post(
        "/strategy", data={"action": "save", "strategy_json": json.dumps(_no_stop_json())}
    )
    assert r.status_code == 400 and "geen stop vanaf de instap" in r.text
    assert (await api.get("/api/strategy")).json()["name"] == DEFAULT_STRATEGY.name

    # The form checks MARKETS like the API.
    other = DEFAULT_STRATEGY.model_dump() | {"market": "XRP-EUR"}
    r = await api.post("/strategy", data={"action": "save", "strategy_json": json.dumps(other)})
    assert r.status_code == 400 and "MARKETS" in r.text

    # A NaN threshold in the form: a clean 400, not a 500.
    nan = DEFAULT_STRATEGY.model_dump_json().replace('"right":35.0', '"right":NaN')
    r = await api.post("/strategy", data={"action": "save", "strategy_json": nan})
    assert r.status_code == 400 and "Ongeldige strategie" in r.text

    r = await api.post("/strategy", data={"action": "save", "strategy_json": "x" * 100_001})
    assert r.status_code == 400 and "te lang" in r.text


@respx.mock
async def test_strategy_page_survives_an_invalid_stored_strategy(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    with SessionLocal() as s:
        s.add(Setting(key=STRATEGY_KEY, value='{"name": "kapot", "buy": [{"right": null}]}'))
        s.commit()
    r = await api.get("/strategy")
    assert r.status_code == 200 and "De opgeslagen strategie is ongeldig" in r.text
    assert (await api.get("/api/strategy")).json()["name"] == DEFAULT_STRATEGY.name
    for page in ("/trading", "/plan", "/"):
        assert (await api.get(page)).status_code == 200, page
    r = await api.post("/strategy", data={"action": "reset", "strategy_json": ""})
    assert r.status_code == 200 and "hersteld en actief" in r.text
    assert "De opgeslagen strategie is ongeldig" not in r.text
    assert "De opgeslagen strategie is ongeldig" not in (await api.get("/strategy")).text


def _add_evaluation(proposal: dict | str | None) -> int:
    with SessionLocal() as s:
        ev = Evaluation(
            model="claude-fable-5-1",
            period_days=7,
            report_nl="Evaluatie",
            proposed_strategy=proposal
            if isinstance(proposal, str) or proposal is None
            else json.dumps(proposal),
        )
        s.add(ev)
        s.commit()
        return ev.id


async def test_applying_an_evaluation_proposal(api):
    no_stop = _add_evaluation(_no_stop_json())
    r = await api.post(f"/api/evaluations/{no_stop}/apply")
    assert r.status_code == 422 and "geen stop vanaf de instap" in r.json()["detail"]
    assert r.json()["code"] == "proposal_not_applicable"
    r = await api.post("/trading/action", data={"action": f"apply:{no_stop}"})
    assert r.status_code == 400 and "geen stop vanaf de instap" in r.text

    other_market = _add_evaluation(DEFAULT_STRATEGY.model_dump() | {"market": "XRP-EUR"})
    r = await api.post("/trading/action", data={"action": f"apply:{other_market}"})
    assert r.status_code == 400 and "XRP-EUR staat niet in MARKETS" in r.text

    broken = _add_evaluation('{"name": "kapot"}')
    assert (await api.post(f"/api/evaluations/{broken}/apply")).status_code == 422

    for bad in ("apply:abc", "apply:", "apply:1:2", "apply:-1", "apply:²", "apply:" + "9" * 20):
        r = await api.post("/trading/action", data={"action": bad})
        assert r.status_code == 400, bad
    assert (await api.post("/api/evaluations/0/apply")).status_code == 422
    assert (await api.post(f"/api/evaluations/{2**40}/apply")).status_code == 422

    good = _add_evaluation(DEFAULT_STRATEGY.model_dump() | {"name": "Beter", "stop_loss_pct": 4})
    page = (await api.get("/trading")).text
    # The owner sees what would change before applying, and why a proposal cannot be applied.
    assert "Wat er verandert als je dit voorstel toepast" in page
    assert "Stop-loss (%)" in page and "Dit voorstel kan niet worden toegepast" in page
    assert f'value="apply:{good}"' in page and f'value="apply:{no_stop}"' not in page

    r = await api.post("/trading/action", data={"action": f"apply:{good}"})
    assert r.status_code == 303
    assert (await api.get("/api/strategy")).json()["name"] == "Beter"


@respx.mock
async def test_market_path_and_query_bounds(api):
    route = respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    # Never reaches Bitvavo: the market goes into the URL path of the Bitvavo call.
    for path in ("/api/candles/..%2Fticker", "/api/chart/..%2F..", "/api/candles/..%2F..%2Fx"):
        assert (await api.get(path)).status_code in (404, 422), path
    for path in (
        "/api/candles/BTC-EUR%3Fx=1",
        "/api/candles/BTC-EUR%23x",
        "/api/candles/BTCEUR",
        "/api/candles/BTC-EUR?limit=0",
        "/api/candles/BTC-EUR?limit=5000",
        "/api/candles/BTC-EUR?interval=1h%2F..",
        "/api/decisions?limit=-1",
        "/api/decisions?limit=100000",
        "/api/journal?limit=-5",
        "/api/journal?days=999999999",
        "/journal?days=999999999",
        "/journal?saved=99999999999999",
        "/api/chart/BTC-EUR?period=" + "x" * 50,
    ):
        assert (await api.get(path)).status_code == 422, path
    assert (await api.post("/api/evaluations?days=99999999")).status_code == 422
    assert (await api.post("/api/evaluations?days=0")).status_code == 422
    assert (await api.put("/api/journal/0/note", json={})).status_code == 422
    # A valid market that is not followed: no call to Bitvavo at all.
    assert (await api.get("/api/candles/XRP-EUR")).status_code == 404
    assert route.call_count == 0
    r = await api.get("/api/candles/btc-eur?limit=10")
    assert r.status_code == 200 and route.call_count == 1
    assert str(route.calls[0].request.url).startswith(f"{BASE}/BTC-EUR/candles")
    assert (await api.get("/api/decisions?limit=5")).status_code == 200
    assert (await api.get("/journal?days=30")).status_code == 200


async def test_settings_api_refuses_nan_and_values_above_ceiling(api):
    r = await api.put(
        "/api/settings",
        content='{"max_trade_eur": NaN}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422 and r.json()["fields"]["max_trade_eur"] == "geen geldig getal"
    r = await api.put("/api/settings", json={"max_trade_eur": 5000})
    assert r.status_code == 422 and "MAX_TRADE_EUR_CEILING" in r.json()["fields"]["max_trade_eur"]
    r = await api.put("/api/settings", json={"news_rss_feeds": ["http://169.254.169.254/"]})
    assert r.status_code == 422
    assert (await api.get("/api/trading/status")).status_code == 200
    r = await api.post(
        "/settings", data={"group": "limits", "max_trade_eur": "nan", "action": "save"}
    )
    assert r.status_code == 400 and "geen geldig getal" in r.text


# -- CSP preparation: no inline scripts or handlers ---------------------------------------------

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
INLINE_HANDLER = re.compile(r"<[^>]+\son[a-z]+\s*=", re.IGNORECASE)


@respx.mock
async def test_pages_have_no_inline_scripts_or_handlers(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    _add_evaluation(DEFAULT_STRATEGY.model_dump() | {"name": "Voorstel"})
    for page in ("/", "/trading", "/journal", "/plan", "/strategy", "/settings", "/news"):
        r = await api.get(page)
        assert r.status_code == 200, page
        assert not INLINE_SCRIPT.search(r.text), page
        assert not INLINE_HANDLER.search(r.text), page
        assert "/static/app.js" in r.text and "/static/theme.js" in r.text
    for js in ("app.js", "theme.js", "chart.js"):
        assert (await api.get(f"/static/{js}")).status_code == 200


async def test_chart_js_escapes_single_quotes(api):
    text = (await api.get("/static/chart.js")).text
    assert "&#39;" in text and "[&<>\"']" in text
