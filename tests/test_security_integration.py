"""Cross-package wiring of the security fixes (integration of the audit packages).

Feed URLs (F21/F45), dashboard and trading warnings (F10 SESSION_SECRET, F33 unprotected
positions, F77 strategy fallback), CSP versus the templates (F22/F28), news query bounds
(F65), single-flight evaluations (F23), chart actions without unfilled orders (F67),
fail-closed limits (F29/F30), confirmed news shocks (F37) and outlet counting (F36/F39).
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app import settings_store as cfg
from app.db import Base, NewsItem, Order, SessionLocal, Setting, init_db, make_engine
from app.main import app
from app.news.service import MARKET_MOOD_KEY
from app.strategy.store import STRATEGY_KEY
from app.trading import risk
from app.trading.engine import EvaluationBusy, news_sources
from app.trading.history import market_actions, trade_actions
from app.trading.risk import RiskLimits
from tests.test_trading import FakeDecider, candles, make_engine_

BASE = "https://api.bitvavo.com/v2"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
INLINE_HANDLER = re.compile(r"<[^>]+\son[a-z]+\s*=", re.IGNORECASE)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _cleanup():
    with SessionLocal() as s:
        s.execute(delete(Setting).where(Setting.key == STRATEGY_KEY))
        s.execute(delete(Setting).where(Setting.key == MARKET_MOOD_KEY))
        s.execute(delete(Setting).where(Setting.key.like(f"{cfg.CONFIG_PREFIX}%")))
        s.commit()


@pytest.fixture
async def api():
    init_db()
    _cleanup()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        _cleanup()


# -- F21/F45: the portal uses the news fetcher's own URL check -------------------------------


@pytest.mark.parametrize(
    "url", ["https://feeds.corp.internal/rss", "https://printer.local/feed", "https://a.lan/x"]
)
def test_settings_refuse_feeds_the_fetcher_would_skip(url):
    with pytest.raises(cfg.SettingsError) as exc:
        cfg.parse_values({"news_rss_feeds": url})
    assert "lokale hostnamen" in exc.value.errors["news_rss_feeds"]


def test_settings_still_accept_public_feeds():
    values = cfg.parse_values({"news_rss_feeds": "https://www.coindesk.com/arc/outboundfeeds/rss/"})
    assert values["news_rss_feeds"] == ["https://www.coindesk.com/arc/outboundfeeds/rss/"]


# -- warnings on the dashboard and the trading page --------------------------------------------


@respx.mock
async def test_dashboard_shows_unprotected_positions_and_weak_secret(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    app.state.engine.unprotected = [
        {
            "id": 7,
            "mode": "live",
            "market": "BTC-EUR",
            "amount": 0.01,
            "stop_price": 40000.0,
            "price": 41000.0,
        }
    ]
    app.state.session_secret_weak = True
    try:
        page = (await api.get("/")).text
        assert "1 open positie wordt NIET bewaakt" in page and "positie #7" in page
        assert "SESSION_SECRET ontbreekt of is korter dan 32 tekens" in page
        trading = (await api.get("/trading")).text
        assert "NIET bewaakt" in trading and "positie #7" in trading
    finally:
        app.state.engine.unprotected = []
        app.state.session_secret_weak = False
    page = (await api.get("/")).text
    assert "NIET bewaakt" not in page and "SESSION_SECRET" not in page


@respx.mock
async def test_dashboard_warns_about_an_invalid_stored_strategy(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    with SessionLocal() as s:
        s.merge(Setting(key=STRATEGY_KEY, value='{"name": "kapot"}'))
        s.commit()
    page = (await api.get("/")).text
    assert "De opgeslagen strategie is ongeldig" in page


# -- F22/F28: CSP script-src 'self' and the templates -------------------------------------------


def test_no_template_has_inline_scripts_or_handlers():
    files = sorted(TEMPLATES.rglob("*.html"))
    assert any(f.parent.name == "auth" for f in files)
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not INLINE_SCRIPT.search(text), f
        assert not INLINE_HANDLER.search(text), f
        assert "javascript:" not in text.lower(), f


@respx.mock
async def test_pages_send_a_csp_that_forbids_inline_scripts(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    for page in ("/", "/trading", "/strategy", "/settings", "/news"):
        r = await api.get(page)
        assert r.status_code == 200, page
        csp = r.headers["content-security-policy"]
        script_src = re.search(r"script-src ([^;]*)", csp).group(1)
        assert script_src.strip() == "'self'", page
        assert not INLINE_SCRIPT.search(r.text), page


# -- F65: news query bounds -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/news?hours=0",
        "/api/news?hours=99999999999",
        "/api/news?limit=0",
        "/api/news?limit=501",
        "/api/news/sentiment?hours=-1",
        "/api/news/sentiment?hours=100000000",
        "/news?hours=100000000",
    ],
)
async def test_news_query_parameters_are_bounded(api, url):
    assert (await api.get(url)).status_code == 422


async def test_news_query_within_bounds(api):
    assert (await api.get("/api/news?hours=720&limit=500")).status_code == 200
    assert (await api.get("/api/news/sentiment?hours=720")).status_code == 200


# -- F23: one evaluation at a time --------------------------------------------------------------


async def test_second_evaluation_while_one_runs_is_refused(session):
    engine = make_engine_(
        session, {"BTC-EUR": candles([10.0] * 60)}, {"BTC-EUR": 10.0}, FakeDecider()
    )
    gate = asyncio.Event()
    original = engine.evaluator.evaluate

    async def slow(sess, context):
        await gate.wait()
        return await original(sess, context)

    engine.evaluator.evaluate = slow
    first = asyncio.create_task(engine.run_evaluation(session))
    await asyncio.sleep(0)
    assert engine.evaluation_busy
    with pytest.raises(EvaluationBusy):
        await engine.run_evaluation(session)
    gate.set()
    assert (await first).id is not None
    assert not engine.evaluation_busy


async def test_evaluation_endpoint_answers_409_while_busy(api, monkeypatch):
    engine = app.state.engine

    async def busy(session, days=7):
        raise EvaluationBusy("Er loopt al een evaluatie; probeer het straks opnieuw.")

    monkeypatch.setattr(type(engine.evaluator), "available", property(lambda self: True))
    monkeypatch.setattr(engine, "run_evaluation", busy)
    r = await api.post("/api/evaluations")
    assert r.status_code == 409 and "Er loopt al een evaluatie" in r.text


# -- F67: chart actions only show orders that moved coins ---------------------------------------


def test_chart_actions_skip_unfilled_orders(session):
    now = datetime.now(UTC)
    since = now - timedelta(days=1)
    for status, amount in (
        ("filled", 0.5),
        ("pending", None),
        ("unknown", None),
        ("rejected", 0.0),
        ("canceled", 0.0),  # cancelled without any fill
    ):
        session.add(
            Order(
                mode="paper",
                market="BTC-EUR",
                side="buy",
                order_type="market",
                amount_quote=50.0,
                amount=amount,
                price=100.0,
                status=status,
                reason="manual",
                created_at=now - timedelta(hours=1),
            )
        )
    session.commit()
    assert len(market_actions(session, "paper", "BTC-EUR", since, "1h")) == 1
    assert len(trade_actions(session, "paper", since, "1h")) == 1


# -- F29/F30: limits fail closed on NaN ---------------------------------------------------------


def test_check_buy_fails_closed_on_nan(session):
    nan = float("nan")
    limits = RiskLimits(nan, 50.0, 5.0, 6, 240, 10.0)
    allowed, why = risk.check_buy(session, limits, "paper", "BTC-EUR", 100, 1000, 0)
    assert allowed == 0 and "geen getal" in why
    ok = RiskLimits(100.0, 50.0, 5.0, 6, 240, 10.0)
    allowed, why = risk.check_buy(session, ok, "paper", "BTC-EUR", 100, nan, 0)
    assert allowed == 0 and "geen getal" in why
    assert risk.check_buy(session, ok, "paper", "BTC-EUR", 100, 1000, 0) == (100.0, None)


# -- F36/F39: one outlet under two names or on two hosts is still one outlet --------------------


def _item(name, url, key="etf"):
    return NewsItem(
        source="rss",
        source_name=name,
        url=url,
        title="Spot ETF approved",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        assessed=True,
        relevant=True,
        sentiment=0.9,
        impact=3,
        horizon="days",
        coins="BTC",
        event_key=key,
    )


def test_news_confirmation_needs_distinct_names_and_sites(session):
    # Two names on one site: the news page counts one outlet, so the engine does too.
    session.add_all(
        [_item("Alpha", "https://www.press.example/a"), _item("Beta", "https://press.example/b")]
    )
    session.commit()
    assert news_sources(session, "BTC", positive=True) == 1
    session.add(_item("Gamma", "https://other.example/c"))
    session.commit()
    assert news_sources(session, "BTC", positive=True) == 2


# -- Fear & Greed: switched off means Claude no longer gets the stored value --------------------


async def test_disabling_fear_greed_drops_the_stored_value(api):
    with SessionLocal() as s:
        s.merge(Setting(key=MARKET_MOOD_KEY, value='{"value": 20, "label": "Fear"}'))
        s.commit()
    r = await api.put("/api/settings", json={"fear_greed_enabled": False})
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        assert s.get(Setting, MARKET_MOOD_KEY) is None


# -- deployment: settings added by the packages reach the container -----------------------------

ROOT = TEMPLATES.parent.parent


@pytest.mark.parametrize(
    "var",
    [
        "SETUP_TOKEN",
        "TRUSTED_ORIGINS",
        "API_DOCS_ENABLED",
        "LIVE_CAPITAL_EUR",
        "EXIT_CHECK_INTERVAL_MIN",
        "MAX_TRADE_EUR_CEILING",
        "MAX_EXPOSURE_PCT_CEILING",
        "MAX_TRADES_PER_DAY_CEILING",
    ],
)
def test_new_settings_are_passed_through_compose_and_documented(var):
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    assert re.search(rf"^\s+{var}: \$\{{{var}:-", compose, re.M), var
    assert re.search(rf"^#? ?{var}=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M)


def test_container_runs_a_single_worker():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"--workers", "1"' in dockerfile
