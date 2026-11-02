"""FastAPI entrypoint: JSON API plus a small dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import errors, flash
from app import settings_store as cfg
from app.ai.budget import BudgetExceeded, BudgetGuard
from app.ai.decision_maker import DecisionMaker
from app.ai.evaluator import Evaluator
from app.ai.news_analyst import NewsAnalyst
from app.ai.strategy_assistant import StrategyAssistant
from app.api_docs import redoc_page, swagger_page
from app.auth.routes import (
    StepUpError,
    _safe_next,
    enrolment_unfinished,
    require_current_password,
    require_login,
    step_up_attempts_left,
    step_up_enabled,
)
from app.auth.routes import router as auth_router
from app.auth.service import (
    AuthService,
    load_or_create_session_secret,
    session_secret_is_weak,
)
from app.bitvavo.broker import PAPER_WALLET_KEY, Broker, LiveBroker, PaperBroker, recent_orders
from app.bitvavo.client import BitvavoClient, BitvavoError, InvalidMarketError
from app.config import Settings, get_settings
from app.db import Evaluation, Position, SessionLocal, Setting, get_session, init_db
from app.news.service import (
    NEWS_RETENTION_DAYS,
    NewsService,
    annotate_groups,
    clamp_future_items,
    clear_market_mood,
    count_status,
    group_items,
    load_market_mood,
    market_sentiment,
    recent_items,
    requeue_items_without_event_key,
)
from app.news.sources import NewsFetcher
from app.notify import Notifier, notify_configured, send_test
from app.security import NO_STORE_PREFIXES, BodySizeLimit, CrossOriginProtection, SecurityHeaders
from app.settings_store import SettingsError
from app.strategy.describe import levels_summary, strategy_diff
from app.strategy.engine import backtest, latest_snapshot
from app.strategy.model import (
    DEFAULT_STRATEGY,
    DESCRIPTION_MAX,
    INTERVALS,
    MAX_CANDLES,
    Strategy,
    check_for_use,
    use_problems,
)
from app.strategy.store import load_strategy, load_strategy_checked, save_strategy
from app.trading import risk
from app.trading.engine import (
    NEWS_ENTRY_MIN_SCORE,
    NEWS_ENTRY_SIZE_FACTOR,
    NEWS_EXIT_MAX_SCORE,
    EvaluationBusy,
    TradingEngine,
    closed_positions,
    latest_decisions,
    open_positions,
    performance_summary,
    recent_decisions,
)
from app.trading.events import parse_calendar
from app.trading.health import bot_health, risk_now
from app.trading.history import (
    PERIODS,
    candle_dicts,
    market_actions,
    period_spec,
    period_start,
    portfolio_history,
    trade_actions,
)
from app.trading.journal import (
    EXIT_NL,
    JournalFilter,
    all_tags,
    journal_entries,
    journal_entry,
    journal_totals,
    save_note,
    unlinked_orders,
)
from app.trading.plan import PLAN_FIELDS, PLAN_MAX_LENGTH, TradingPlan, load_plan, save_plan
from app.trading.risk import RiskLimits
from app.trading.trend import load_trends, refresh_trends
from app.web import STATIC_DIR, eur, num, templates

log = logging.getLogger("crypie")
# Uvicorn only configures its own loggers; without this the cycle and news results never show
# up in the container logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, query string included (the CryptoPanic auth_token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def live_blocker(
    settings: Settings, client: BitvavoClient, *, for_exits: bool = False
) -> str | None:
    """Why live trading is not allowed right now, or None when it is.

    All conditions live in the environment on purpose: switching to live in the portal is
    never enough on its own. ``for_exits`` skips the login condition: it guards opening
    trades, while protective sells of open live positions must keep running without a login.
    """
    if not for_exits and not settings.auth_enabled:
        return "AUTH_ENABLED staat op false; live handelen vereist een login"
    if not client.authenticated:
        return "er is geen Bitvavo-sleutel (BITVAVO_API_KEY en BITVAVO_API_SECRET) ingesteld"
    if not settings.live_trading_ack:
        return "LIVE_TRADING_ACK staat niet op true in de serveromgeving"
    return None


def build_broker(settings: Settings, client: BitvavoClient) -> Broker:
    if settings.trading_mode == "live":
        blocker = live_blocker(settings, client)
        if blocker:
            raise RuntimeError(f"live handelen is niet toegestaan: {blocker}")
        return LiveBroker(client)
    return PaperBroker(client, settings.paper_start_eur, settings.trading_fee_pct)


def exit_broker(app: FastAPI, mode: str) -> Broker | None:
    """The broker that protects open positions of ``mode`` when it is not the active mode.

    Live positions stay protected after a switch to paper for as long as the environment
    still allows live trading (key plus LIVE_TRADING_ACK); otherwise None: unprotected.
    """
    state = app.state
    if mode == "live":
        if live_blocker(state.env, state.client, for_exits=True):
            return None
        return LiveBroker(state.client)
    if mode == "paper":
        settings: Settings = state.settings
        return PaperBroker(state.client, settings.paper_start_eur, settings.trading_fee_pct)
    return None


def build_auth(settings: Settings) -> AuthService | None:
    """Login is on unless AUTH_ENABLED=false; the account itself is created at /setup (with the
    setup link from the log) or with ``python -m app.auth create-account``."""
    if not settings.auth_enabled:
        log.warning("AUTH_ENABLED=false: the dashboard is reachable without logging in")
        return None
    if session_secret_is_weak(settings):
        log.warning(
            "SESSION_SECRET is %s: whoever can read the database can forge login cookies; "
            "set it to a random value of at least 32 characters",
            "not set" if not settings.session_secret else "shorter than 32 characters",
        )
    secret = settings.session_secret
    with SessionLocal() as session:
        if not secret:
            secret = load_or_create_session_secret(session)
        auth = AuthService(settings, secret)
        auth.ensure_account_id(session)
        if not auth.has_account(session):
            auth.log_setup_hint()
        elif enrolment_unfinished(auth.user(session)):
            # The first linking was never finished: the setup link plus the password restarts it.
            log.warning(
                "the account has no authenticator linked yet: open /setup?token=%s and log in "
                "with the password to link one, or run: python -m app.auth reset-mfa",
                "<SETUP_TOKEN>" if auth.setup_token_from_env else auth.setup_token,
            )
        elif not auth.user(session).mfa_enrolled:
            log.warning("the account has no authenticator linked yet")
    return auth


def apply_settings(app: FastAPI, new: Settings) -> None:
    """Push new settings into the running components, so a change works without a restart.

    The caller has validated ``new`` (including the live-trading safeguard).
    """
    state = app.state
    old: Settings = state.settings
    if new.trading_mode != state.broker.mode or (
        new.trading_mode == "paper" and new.paper_start_eur != old.paper_start_eur
    ):
        state.broker = build_broker(new, state.client)
        state.engine.broker = state.broker
    state.settings = new
    state.budget.limit = new.claude_monthly_budget_usd
    state.assistant.model = new.claude_model_strategy
    engine: TradingEngine = state.engine
    engine.markets = list(new.markets)
    engine.limits = RiskLimits(
        new.max_trade_eur,
        new.max_exposure_pct,
        new.daily_loss_limit_pct,
        new.max_trades_per_day,
        new.cooldown_min_after_stop,
        new.min_trade_eur,
    )
    engine.claude_unavailable_mode = new.claude_unavailable_mode
    engine.backtest_fee_pct = new.backtest_fee_pct
    engine.fee_pct = new.trading_fee_pct
    engine.watch_max_min = new.watch_max_min
    engine.trend_context = new.trend_context_enabled
    engine.market_mood_enabled = new.fear_greed_enabled
    engine.live_capital_eur = new.live_capital_eur
    _apply_risk_off(engine, new)
    if new.watch_interval_min <= 0:
        engine.watch.clear()
    if isinstance(state.broker, PaperBroker):
        state.broker.fee_pct = new.trading_fee_pct
    engine.decision_maker.model = new.claude_model_decision
    engine.evaluator.model = new.claude_model_evaluation
    news: NewsService = state.news
    news.analyst.model = new.claude_model_news
    news.coins = new.coins
    news.max_age_hours = new.news_max_age_hours
    news.batch_size = new.news_batch_size
    fetcher: NewsFetcher = news.fetcher
    fetcher.rss_feeds = list(new.news_rss_feeds)
    fetcher.gdelt_query = new.gdelt_query if new.gdelt_enabled else None
    fetcher.cryptopanic_plan = new.cryptopanic_plan
    fetcher.coins = new.coins
    fetcher.cryptocv_enabled = new.cryptocv_enabled
    fetcher.google_news_enabled = new.google_news_enabled
    fetcher.fear_greed_enabled = new.fear_greed_enabled


def _apply_risk_off(engine: TradingEngine, settings: Settings) -> None:
    engine.calendar = parse_calendar(settings.event_calendar)
    engine.risk_off_enabled = settings.risk_off_enabled
    engine.risk_off_before_min = settings.risk_off_before_min
    engine.risk_off_after_min = settings.risk_off_after_min
    engine.shock_pause_hours = settings.shock_pause_hours


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = get_settings()
    init_db()
    app.state.env = env
    app.state.config_warning = None
    # Login settings come from the environment only; they are not editable in the portal.
    app.state.auth = build_auth(env)
    app.state.session_secret_weak = env.auth_enabled and session_secret_is_weak(env)
    flash.configure(_flash_secret(env, app.state.auth))
    with SessionLocal() as session:
        settings = cfg.effective_settings(session, env)
        requeued = requeue_items_without_event_key(session)
        if requeued:
            log.info("news: %d items re-queued for an event key", requeued)
        try:
            # Stored articles dated in the future (from before the ingestion clamp) would keep
            # a shock pause or sentiment alive; the news job may not run (news switched off).
            clamped = clamp_future_items(session)
            if clamped:
                log.info("news: %d future-dated items clamped", clamped)
        except Exception:  # noqa: BLE001 - never block startup on housekeeping
            session.rollback()
            log.exception("clamping future-dated news at startup failed")
    client = BitvavoClient(env.bitvavo_api_key, env.bitvavo_api_secret, env.bitvavo_rest_url)
    app.state.client = client
    if client.authenticated:
        # Use the account's real taker fee when Bitvavo tells us; otherwise the configured one.
        # Best-effort: an unreachable Bitvavo must never keep the app (and its kill switch)
        # from starting.
        try:
            real_fee = await asyncio.wait_for(client.taker_fee_pct(), FEE_PROBE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("Bitvavo fee probe failed (%s); using TRADING_FEE_PCT", exc)
            real_fee = None
        if real_fee is not None and real_fee != settings.trading_fee_pct:
            log.info("Bitvavo taker fee is %.4f%%, overriding TRADING_FEE_PCT", real_fee)
            settings = settings.model_copy(update={"trading_fee_pct": real_fee})
    try:
        broker = build_broker(settings, client)
    except RuntimeError as exc:
        # Never refuse to start: run in paper mode and say why, loudly, in the portal.
        log.warning("%s; starting in paper mode instead", exc)
        app.state.config_warning = (
            f"Live-modus is niet gestart omdat {exc.args[0].split(': ', 1)[-1]}. "
            "De app draait in oefenmodus."
        )
        if env.trading_mode != "live":
            with SessionLocal() as session:
                cfg.reset_overrides(session, ["trading_mode"])
        settings = settings.model_copy(update={"trading_mode": "paper"})
        broker = PaperBroker(client, settings.paper_start_eur, settings.trading_fee_pct)
    app.state.settings = settings
    app.state.broker = broker
    app.state.budget = BudgetGuard(settings.claude_monthly_budget_usd)
    app.state.assistant = StrategyAssistant(
        settings.anthropic_api_key, settings.claude_model_strategy, app.state.budget
    )
    fetcher = NewsFetcher(
        settings.news_rss_feeds,
        settings.gdelt_query if settings.gdelt_enabled else None,
        settings.cryptopanic_token,
        settings.cryptopanic_plan,
        settings.coins,
        cryptocv_enabled=settings.cryptocv_enabled,
        google_news_enabled=settings.google_news_enabled,
        fear_greed_enabled=settings.fear_greed_enabled,
    )
    app.state.news = NewsService(
        fetcher,
        NewsAnalyst(settings.anthropic_api_key, settings.claude_model_news, app.state.budget),
        settings.coins,
        settings.news_max_age_hours,
        settings.news_batch_size,
    )
    app.state.engine = TradingEngine(
        client,
        app.state.broker,
        DecisionMaker(settings.anthropic_api_key, settings.claude_model_decision, app.state.budget),
        Evaluator(settings.anthropic_api_key, settings.claude_model_evaluation, app.state.budget),
        settings.markets,
        RiskLimits(
            settings.max_trade_eur,
            settings.max_exposure_pct,
            settings.daily_loss_limit_pct,
            settings.max_trades_per_day,
            settings.cooldown_min_after_stop,
            settings.min_trade_eur,
        ),
        settings.claude_unavailable_mode,
        settings.backtest_fee_pct,
        settings.trading_fee_pct,
        settings.watch_max_min,
    )
    engine: TradingEngine = app.state.engine
    engine.trend_context = settings.trend_context_enabled
    engine.market_mood_enabled = settings.fear_greed_enabled
    engine.live_capital_eur = settings.live_capital_eur
    engine.broker_for_mode = lambda mode: exit_broker(app, mode)
    _apply_risk_off(engine, settings)
    # Notifications are configured in the environment only (NOTIFY_URL); off without it.
    notifier = Notifier(env)
    app.state.notifier = notifier
    engine.notifier = notifier
    with SessionLocal() as session:
        engine.load_state(session)
        if engine.last_run is None:
            # No cycle time kept in the engine state yet: the scheduler's start time is close.
            engine.last_run = _load_last_run("trading cycle")
        # Orders sent before a crash or restart whose outcome was never recorded.
        try:
            settled = await asyncio.wait_for(engine.reconcile(session), RECONCILE_TIMEOUT_S)
            if settled:
                log.info("reconciled %d order(s) at startup", settled)
        except Exception:  # noqa: BLE001 - retried by the exit loop
            log.exception("order reconciliation at startup failed")
        _notify_start(app, session)
    # The schedulers read the interval and on/off switch from app.state.settings on every
    # tick, so a change in the portal takes effect within a tick, without a restart.
    tasks = [
        asyncio.create_task(exit_scheduler(app)),
        asyncio.create_task(news_scheduler(app)),
        asyncio.create_task(trading_scheduler(app)),
        asyncio.create_task(watch_scheduler(app)),
        asyncio.create_task(trend_scheduler(app)),
        asyncio.create_task(evaluation_scheduler(app)),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await notifier.drain(timeout=2)
        await fetcher.aclose()
        await client.aclose()


def _flash_secret(env: Settings, auth: AuthService | None) -> str | None:
    """The secret the flash cookies are signed with: SESSION_SECRET, else the stored session
    secret when the login is on; None means a random key per process."""
    if env.session_secret:
        return env.session_secret
    if auth is None:
        return None
    try:
        with SessionLocal() as session:
            return load_or_create_session_secret(session)
    except Exception:  # noqa: BLE001
        log.warning("could not read the session secret for flash messages")
        return None


def _notify_start(app: FastAPI, session: Session) -> None:
    """Tell the owner the app (re)started: an unexpected restart is worth knowing about."""
    notifier: Notifier = app.state.notifier
    if not notifier.enabled:
        return
    mode = app.state.broker.mode
    try:
        n = int(session.scalar(select(func.count(Position.id)).where(Position.status == "open")))
    except Exception:  # noqa: BLE001
        session.rollback()
        n = None
    text = "Crypie is gestart in " + (
        "live-modus (echt geld)." if mode == "live" else "oefenmodus."
    )
    if app.state.config_warning:
        text += " Let op: live-modus is niet gestart, de app draait in oefenmodus."
    if n is not None:
        text += f" Er {'staat' if n == 1 else 'staan'} {n} open " + (
            "positie." if n == 1 else "posities."
        )
    notifier.restart(session, text)


SCHEDULER_TICK_S = 30
SCHED_LAST_PREFIX = "sched:last:"
FEE_PROBE_TIMEOUT_S = 5.0
RECONCILE_TIMEOUT_S = 60.0
JOB_TIMEOUT_S = 1800.0  # no scheduled job may hang forever


def _load_last_run(name: str) -> datetime | None:
    """When ``name`` last started, as stored in the database (None when unknown)."""
    try:
        with SessionLocal() as session:
            row = session.get(Setting, SCHED_LAST_PREFIX + name)
            if row is None:
                return None
            when = datetime.fromisoformat(row.value)
    except Exception:  # noqa: BLE001 - unreadable state means "unknown", never a crash
        log.warning("ignoring unreadable scheduler state for %s", name)
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _save_last_run(name: str, when: datetime) -> None:
    try:
        with SessionLocal() as session:
            row = session.get(Setting, SCHED_LAST_PREFIX + name)
            if row is None:
                session.add(Setting(key=SCHED_LAST_PREFIX + name, value=when.isoformat()))
            else:
                row.value = when.isoformat()
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not store scheduler state for %s", name)


async def _run_periodically(
    app: FastAPI,
    name: str,
    job,
    seconds_between,
    initial_delay: float,
    run_at_start: bool = True,
    persist: bool = True,
    timeout: float | None = JOB_TIMEOUT_S,
) -> None:
    """Run ``job(session)`` whenever ``seconds_between(settings)`` has passed since the last run.

    ``seconds_between`` returns 0 or less to switch the job off; it is re-read every tick.
    With ``run_at_start`` False the first run waits one full interval. With ``persist`` the
    start time of the last run is kept in the database, so a restart waits for the next due
    time instead of running the job (and asking Claude) again right away. ``timeout`` stops a
    run that hangs.
    """
    await asyncio.sleep(initial_delay)
    last = _load_last_run(name) if persist else None
    now = datetime.now(UTC)
    if last is not None and last > now:
        last = now  # a clock that went back: wait one interval rather than never run
    if last is None and not run_at_start:
        last = now
        if persist:
            _save_last_run(name, now)
    while True:
        interval = seconds_between(app.state.settings)
        now = datetime.now(UTC)
        if interval > 0 and (last is None or (now - last).total_seconds() >= interval):
            last = now
            if persist:
                _save_last_run(name, now)  # before the run: a crash loop does not re-run it
            try:
                with SessionLocal() as session:
                    await asyncio.wait_for(job(session), timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                log.error("%s took longer than %.0f s and was stopped", name, timeout or 0)
            except Exception:
                log.exception("%s failed", name)
        await asyncio.sleep(SCHEDULER_TICK_S)


async def exit_scheduler(app: FastAPI) -> None:
    """Every EXIT_CHECK_INTERVAL_MIN: settle unknown orders, then check the stop-loss, trailing
    stop and take-profit of every open position.

    Deliberately independent of TRADING_ENABLED, the kill switch, MARKETS, candles and Claude:
    a position must stay protected whatever else is switched off or broken.
    """

    async def job(session: Session) -> None:
        engine: TradingEngine = app.state.engine
        # Settles unknown orders first; a failure there does not skip the stop checks.
        results = await engine.exit_check(session)
        if results:
            log.info("exit check: %s", [r.as_dict() for r in results])

    await _run_periodically(
        app,
        "exit check",
        job,
        lambda s: max(s.exit_check_interval_min, 1) * 60,
        initial_delay=15,
        persist=False,
        timeout=300,
    )


async def news_scheduler(app: FastAPI) -> None:
    """Fetch and assess news every NEWS_FETCH_INTERVAL_MIN minutes for as long as the app runs."""

    async def job(session: Session) -> None:
        log.info("news refresh: %s", await app.state.news.refresh(session))

    await _run_periodically(
        app,
        "news refresh",
        job,
        lambda s: s.news_fetch_interval_min * 60 if s.news_enabled else 0,
        initial_delay=5,  # let the server come up first
    )


async def trading_scheduler(app: FastAPI) -> None:
    """Run one trading cycle per TRADE_INTERVAL_MIN."""

    async def job(session: Session) -> None:
        results = await app.state.engine.run_cycle(session)
        log.info("trading cycle: %s", [r.as_dict() for r in results])

    await _run_periodically(
        app,
        "trading cycle",
        job,
        lambda s: s.trade_interval_min * 60 if s.trading_enabled else 0,
        initial_delay=20,  # let news land first
        timeout=None,  # every market already has its own deadline
    )


async def watch_scheduler(app: FastAPI) -> None:
    """Re-check markets in dip watch every WATCH_INTERVAL_MIN minutes (only while any is armed)."""

    async def job(session: Session) -> None:
        engine: TradingEngine = app.state.engine
        if engine.watch:
            results = await engine.run_watch(session)
            log.info("dip watch: %s", [r.as_dict() for r in results])

    await _run_periodically(
        app,
        "dip watch",
        job,
        lambda s: (
            s.watch_interval_min * 60 if s.trading_enabled and s.watch_interval_min > 0 else 0
        ),
        initial_delay=60,
        run_at_start=False,
        timeout=None,  # every market already has its own deadline
    )


async def trend_scheduler(app: FastAPI) -> None:
    """Refresh the long-term trend (200-week MA, monthly RSI) per market once a day."""

    async def job(session: Session) -> None:
        engine: TradingEngine = app.state.engine
        trends = await refresh_trends(engine.client, session, list(engine.markets))
        log.info("trend refresh: %s", [t["summary_nl"] for t in trends])

    await _run_periodically(
        app,
        "trend refresh",
        job,
        lambda s: 24 * 3600 if s.trading_enabled and s.trend_context_enabled else 0,
        initial_delay=10,  # before the first trading cycle (at 20 s)
    )


async def evaluation_scheduler(app: FastAPI) -> None:
    """Let Claude evaluate the results every EVALUATION_INTERVAL_HOURS, the first time after
    one full interval."""

    async def job(session: Session) -> None:
        ev = await app.state.engine.run_evaluation(session)
        log.info("evaluation %s stored", ev.id)

    await _run_periodically(
        app,
        "evaluation",
        job,
        lambda s: s.evaluation_interval_hours * 3600 if s.trading_enabled else 0,
        initial_delay=30,
        run_at_start=False,
    )


# Versioned static files (?v=<ASSET_VERSION>, ?v=5.2.1) never change under the same URL.
STATIC_IMMUTABLE = "public, max-age=31536000, immutable"


class StaticCacheHeaders:
    """Long-lived caching for versioned files under /static/; the rest is revalidated (ETag)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/static/"):
            await self.app(scope, receive, send)
            return
        query = scope.get("query_string", b"").decode("latin-1")
        versioned = any(part.startswith("v=") and len(part) > 2 for part in query.split("&"))

        async def send_cached(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in (200, 304):
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = STATIC_IMMUTABLE if versioned else "no-cache"
            await send(message)

        await self.app(scope, receive, send_cached)


class Compression:
    """Gzip for pages, JSON and static files, except the login and account pages: those hold
    secrets (an authenticator key, recovery codes) and stay uncompressed (BREACH)."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not scope.get("path", "").startswith(NO_STORE_PREFIXES):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


_docs = get_settings().api_docs_enabled  # API docs only on request, and behind the login
app = FastAPI(
    title="Crypie",
    version="0.1.0",
    lifespan=lifespan,
    # /docs and /redoc are served below with pinned, SRI-checked scripts (app/api_docs.py).
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)
if _docs:
    app.get("/docs", include_in_schema=False)(lambda: swagger_page(app.title, app.openapi_url))
    app.get("/redoc", include_in_schema=False)(lambda: redoc_page(app.title, app.openapi_url))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.middleware("http")(require_login)
# Added last = runs first: compression around everything, headers on every response (also
# the error pages), cache headers for /static, then the body limit and the origin check, all
# before the login check and before any body is parsed. Inside those: the 500 page, and the
# one-time messages (flash cookie) around the login check and the routes.
app.add_middleware(flash.FlashMiddleware)
app.add_middleware(errors.ServerErrorPages)
app.add_middleware(CrossOriginProtection)
app.add_middleware(BodySizeLimit)
app.add_middleware(StaticCacheHeaders)
app.add_middleware(SecurityHeaders)
app.add_middleware(Compression)
errors.install(app)
app.include_router(auth_router)


def get_client(request: Request) -> BitvavoClient:
    return request.app.state.client


def get_broker(request: Request) -> Broker:
    return request.app.state.broker


def get_assistant(request: Request) -> StrategyAssistant:
    return request.app.state.assistant


def get_budget(request: Request) -> BudgetGuard:
    return request.app.state.budget


def get_news(request: Request) -> NewsService:
    return request.app.state.news


def get_engine(request: Request) -> TradingEngine:
    return request.app.state.engine


# -- API -------------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/prices")
async def prices(request: Request, client: BitvavoClient = Depends(get_client)):
    try:
        return await client.ticker_prices(request.app.state.settings.markets)
    except (BitvavoError, Exception) as exc:
        raise HTTPException(502, f"Bitvavo unreachable: {exc}") from exc


# Path and query bounds. A market goes into the Bitvavo URL path, so only the strict
# Bitvavo notation is accepted (no '..', '?' or '#'); ids fit the database's integer columns.
MARKET_PATH = Path(pattern=r"^[A-Za-z0-9]{2,10}-[Ee][Uu][Rr]$")
DB_ID_MAX = 2**31 - 1
INTERVAL_PATTERN = "^(" + "|".join((*INTERVALS, "1W", "1M")) + ")$"  # what Bitvavo offers


def _tracked_market(request: Request, market: str) -> str:
    market = market.upper()
    if market not in request.app.state.settings.markets:
        raise HTTPException(404, f"Markt {market} wordt niet gevolgd")
    return market


@app.get("/api/candles/{market}")
async def candles(
    request: Request,
    market: str = MARKET_PATH,
    interval: str = Query("1h", pattern=INTERVAL_PATTERN),
    limit: int = Query(100, ge=1, le=MAX_CANDLES),
    client: BitvavoClient = Depends(get_client),
):
    market = _tracked_market(request, market)
    try:
        return await client.candles(market, interval, limit)
    except InvalidMarketError as exc:
        raise HTTPException(400, str(exc)) from exc
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/chart/{market}")
async def chart_market(
    request: Request,
    market: str = MARKET_PATH,
    period: str = Query("7d", max_length=8),
    client: BitvavoClient = Depends(get_client),
    session: Session = Depends(get_session),
):
    """Candles of one market plus the bot's actions in that window, for the overview chart."""
    market = _tracked_market(request, market)
    key, interval, limit = period_spec(period)
    try:
        rows = await client.candles(market, interval, limit)
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc
    candles_ = candle_dicts(rows)
    since = (
        datetime.fromtimestamp(candles_[0]["time"], UTC)
        if candles_
        else period_start(interval, limit)
    )
    mode = request.app.state.engine.mode
    return {
        "market": market,
        "period": key,
        "interval": interval,
        "candles": candles_,
        "actions": market_actions(session, mode, market, since, interval),
    }


@app.get("/api/portfolio/history")
async def portfolio_history_api(
    request: Request,
    period: str = Query("7d", max_length=8),
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    session: Session = Depends(get_session),
):
    """Portfolio value per candle, rebuilt from today's balance and the order history."""
    settings: Settings = request.app.state.settings
    key, interval, limit = period_spec(period)
    try:
        results = await asyncio.gather(
            *(client.candles(m, interval, limit) for m in settings.markets)
        )
        balances = await broker.balances(session)
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc
    candles_by_market = {
        m: candle_dicts(rows) for m, rows in zip(settings.markets, results, strict=True)
    }
    starts = [rows[0]["time"] for rows in candles_by_market.values() if rows]
    since = datetime.fromtimestamp(min(starts), UTC) if starts else period_start(interval, limit)
    mode = request.app.state.engine.mode
    return {
        "period": key,
        "interval": interval,
        "mode": mode,
        "start_eur": settings.paper_start_eur if mode == "paper" else None,
        "points": portfolio_history(session, mode, balances, candles_by_market, since),
        "actions": trade_actions(session, mode, since, interval),
    }


@app.get("/api/balance")
async def balance(broker: Broker = Depends(get_broker), session: Session = Depends(get_session)):
    try:
        return {"mode": broker.mode, "balances": await broker.balances(session)}
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc


# Step-up over the API: the current password in a header, for calls without a JSON body.
PASSWORD_HEADER = "x-current-password"


class OrderIn(BaseModel):
    market: str = Field(max_length=32)
    side: Literal["buy", "sell"]
    # Range and finiteness are checked by TradingEngine.manual_order (400 with a reason); a
    # pydantic error would echo a NaN input back and fail to serialise.
    amount_quote: float = Field(description="Amount in EUR")
    reason: str | None = Field(default=None, max_length=200)
    # Needed while the bot trades live (step-up, like the /account page).
    current_password: str | None = Field(default=None, max_length=1024)


@app.get("/api/orders")
async def list_orders(session: Session = Depends(get_session)):
    return [
        {
            "id": o.id,
            "created_at": o.created_at,
            "mode": o.mode,
            "market": o.market,
            "side": o.side,
            "amount": o.amount,
            "amount_quote": o.amount_quote,
            "fee_quote": o.fee_quote,
            "price": o.price,
            "status": o.status,
            "reason": o.reason,
        }
        for o in recent_orders(session)
    ]


@app.post("/api/orders", status_code=201)
async def create_order(
    order: OrderIn,
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """A manual order under the bot's rules: see ``TradingEngine.manual_order``.

    While the bot trades live the current password is needed as well.
    """
    if engine.mode == "live":
        password = order.current_password or request.headers.get(PASSWORD_HEADER)
        try:
            await require_current_password(request, session, password)
        except StepUpError as exc:
            raise HTTPException(exc.status, exc.message) from exc
    try:
        o = await engine.manual_order(
            session, order.market, order.side, order.amount_quote, order.reason
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "id": o.id,
        "status": o.status,
        "price": o.price,
        "amount": o.amount,
        "amount_quote": o.amount_quote,
        "fee_quote": o.fee_quote,
        "mode": o.mode,
    }


# -- Strategy ------------------------------------------------------------------------


class ProposalIn(BaseModel):
    description: str = Field(min_length=5, max_length=DESCRIPTION_MAX)


class BacktestIn(BaseModel):
    strategy: Strategy | None = None
    limit: int = Field(default=500, ge=50, le=MAX_CANDLES)
    start_eur: float = Field(default=1000, gt=0, le=1e9, allow_inf_nan=False)


# The JSON of the largest valid strategy is far below this; the form refuses anything bigger.
STRATEGY_JSON_MAX = 100_000


async def _run_backtest(
    request: Request, client: BitvavoClient, strategy: Strategy, limit: int, start_eur: float
) -> dict:
    candles = await client.candles(strategy.market, strategy.interval, limit)
    result = backtest(strategy, candles, start_eur, request.app.state.settings.backtest_fee_pct)
    return {
        **result.summary(),
        "market": strategy.market,
        "interval": strategy.interval,
        "latest": latest_snapshot(strategy, candles),
        "trade_log": [
            {
                "entry_time": t.entry_time,
                "entry_price": t.entry_price,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price,
                "pnl_pct": round(t.pnl_pct, 2) if t.pnl_pct is not None else None,
                "reason": t.reason,
            }
            for t in result.trades[-20:]
        ],
    }


@app.get("/api/strategy")
async def get_strategy(session: Session = Depends(get_session)):
    return load_strategy(session).model_dump()


@app.put("/api/strategy")
async def put_strategy(
    strategy: Strategy, request: Request, session: Session = Depends(get_session)
):
    if strategy.market not in request.app.state.settings.markets:
        raise HTTPException(400, f"{strategy.market} is not in MARKETS")
    try:
        check_for_use(strategy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_strategy(session, strategy)
    return strategy.model_dump()


@app.post("/api/strategy/propose")
async def propose_strategy(
    body: ProposalIn,
    request: Request,
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    if not assistant.available:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured")
    try:
        proposal = await assistant.propose(
            session, body.description, load_strategy(session), request.app.state.settings.markets
        )
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"strategy": proposal.strategy.model_dump(), "explanation": proposal.explanation}


@app.get("/api/claude/usage")
async def claude_usage(
    budget: BudgetGuard = Depends(get_budget), session: Session = Depends(get_session)
):
    return budget.status(session)


@app.post("/api/backtest")
async def run_backtest(
    body: BacktestIn,
    request: Request,
    client: BitvavoClient = Depends(get_client),
    session: Session = Depends(get_session),
):
    strategy = body.strategy or load_strategy(session)
    try:
        return await _run_backtest(request, client, strategy, body.limit, body.start_eur)
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, f"backtest failed: {exc}") from exc


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(
    request: Request,
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    strategy, load_warning = load_strategy_checked(session)
    return templates.TemplateResponse(
        request,
        "strategy.html",
        {
            "settings": request.app.state.settings,
            "strategy_json": strategy.model_dump_json(indent=2),
            "strategy": strategy,
            "load_warning": load_warning,
            "assistant_available": assistant.available,
            "proposal": None,
            "proposal_diff": None,
            "explanation": None,
            "backtest": None,
            "error": None,
            "message": None,
        },
    )


@app.post("/strategy", response_class=HTMLResponse)
async def strategy_action(
    request: Request,
    action: str = Form(..., max_length=32),
    strategy_json: str = Form(""),
    description: str = Form(""),
    limit: int = Form(500, ge=50, le=MAX_CANDLES),
    client: BitvavoClient = Depends(get_client),
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    current, load_warning = load_strategy_checked(session)
    ctx: dict = {
        "settings": settings,
        "strategy_json": strategy_json[:STRATEGY_JSON_MAX],
        "strategy": current,
        "load_warning": load_warning,
        "assistant_available": assistant.available,
        "proposal": None,
        "proposal_diff": None,
        "explanation": None,
        "backtest": None,
        "error": None,
        "message": None,
    }
    status = 200
    try:
        if action == "propose":
            if not assistant.available:
                raise RuntimeError("ANTHROPIC_API_KEY is niet ingesteld")
            # Same bounds as POST /api/strategy/propose.
            ProposalIn(description=description)
            proposal = await assistant.propose(
                session, description, ctx["strategy"], settings.markets
            )
            ctx["strategy_json"] = proposal.strategy.model_dump_json(indent=2)
            ctx["proposal"] = proposal.strategy
            ctx["proposal_diff"] = strategy_diff(current, proposal.strategy)
            ctx["explanation"] = proposal.explanation
            ctx["description"] = description
            ctx["backtest"] = await _run_backtest(request, client, proposal.strategy, limit, 1000)
            ctx["message"] = (
                "Claude heeft een voorstel gemaakt. "
                "Bekijk het hieronder en sla het op als je het goed vindt."
            )
            problems = use_problems(proposal.strategy)
            if problems:
                ctx["error"] = (
                    "Dit voorstel kan zo niet worden opgeslagen: " + "; ".join(problems) + "."
                )
        elif action == "reset":
            save_strategy(session, DEFAULT_STRATEGY)
            ctx["strategy"] = DEFAULT_STRATEGY
            ctx["load_warning"] = None
            ctx["strategy_json"] = DEFAULT_STRATEGY.model_dump_json(indent=2)
            ctx["message"] = f"Standaardstrategie '{DEFAULT_STRATEGY.name}' hersteld en actief."
        else:
            if len(strategy_json) > STRATEGY_JSON_MAX:
                raise ValueError(
                    f"De strategie-JSON is te lang (maximaal {STRATEGY_JSON_MAX} tekens)."
                )
            candidate = Strategy.model_validate_json(strategy_json)
            if candidate.market not in settings.markets:
                raise ValueError(f"{candidate.market} staat niet in MARKETS")
            if action == "backtest":
                ctx["proposal"] = candidate
                ctx["proposal_diff"] = strategy_diff(current, candidate)
                ctx["backtest"] = await _run_backtest(request, client, candidate, limit, 1000)
            elif action == "save":
                check_for_use(candidate)
                save_strategy(session, candidate)
                ctx["strategy"] = candidate
                ctx["load_warning"] = None
                ctx["message"] = f"Strategie '{candidate.name}' opgeslagen en actief."
            else:
                raise ValueError(f"Onbekende actie {action!r}")
    except ValidationError as exc:
        what = "Ongeldige omschrijving" if action == "propose" else "Ongeldige strategie"
        ctx["error"] = f"{what}: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        status = 400
    except (ValueError, RuntimeError, ArithmeticError, BitvavoError) as exc:
        ctx["error"] = str(exc)
        status = 400
    return templates.TemplateResponse(request, "strategy.html", ctx, status_code=status)


# -- News -----------------------------------------------------------------------------


def _news_dict(it, tracked: list[str]) -> dict:
    status = count_status(it, tracked)
    return {
        "id": it.id,
        "source": it.source,
        "source_name": it.source_name,
        "title": it.title,
        "url": it.url,
        "published_at": it.published_at,
        "assessed": it.assessed,
        "relevant": it.relevant,
        "sentiment": it.sentiment,
        "impact": it.impact,
        "horizon": it.horizon,
        "coins": it.coin_list(),
        "summary_nl": it.summary_nl,
        "event_key": it.event_key,
        "counts": status.counts,  # weighs in the market sentiment right now
        "counts_reason": status.reason_nl,
    }


# News is only kept this long; larger windows would only overflow timedelta.
NEWS_HOURS_MAX = NEWS_RETENTION_DAYS * 24


@app.get("/api/news")
async def list_news(
    request: Request,
    hours: int = Query(24, ge=1, le=NEWS_HOURS_MAX),
    limit: int = Query(100, ge=1, le=500),
    counts: bool | None = None,
    session: Session = Depends(get_session),
):
    tracked = request.app.state.settings.coins
    rows = [_news_dict(it, tracked) for it in recent_items(session, hours, limit)]
    if counts is not None:
        rows = [r for r in rows if r["counts"] == counts]
    return rows


@app.post("/api/news/refresh")
async def refresh_news(
    news: NewsService = Depends(get_news), session: Session = Depends(get_session)
):
    return await news.refresh(session)


@app.get("/api/news/sentiment")
async def news_sentiment(
    request: Request,
    hours: int = Query(48, ge=1, le=NEWS_HOURS_MAX),
    session: Session = Depends(get_session),
):
    return {
        coin: market_sentiment(session, coin, hours).as_dict()
        for coin in request.app.state.settings.coins
    }


@app.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    hours: int = Query(24, ge=1, le=NEWS_HOURS_MAX),
    # "ja" = only facts that weigh in the sentiment, "nee" = only the rest
    telt: str = Query("", max_length=8),
    news: NewsService = Depends(get_news),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    counted = {"ja": True, "nee": False}.get(telt)
    groups = annotate_groups(group_items(recent_items(session, hours, 150)), settings.coins)
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "settings": settings,
            "hours": hours,
            "telt": telt if counted is not None else "",
            "groups": [g for g in groups if counted is None or g.status.counts == counted],
            "n_counted": sum(1 for g in groups if g.status.counts),
            "n_total": len(groups),
            "sentiments": [market_sentiment(session, c, 48) for c in settings.coins],
            "market_mood": load_market_mood(session) if settings.fear_greed_enabled else None,
            "last_run": news.last_run,
            "last_error": news.last_error,
            "analyst_available": news.analyst.available,
        },
    )


@app.post("/news/refresh")
async def news_refresh_form(
    news: NewsService = Depends(get_news), session: Session = Depends(get_session)
):
    await news.refresh(session)
    return RedirectResponse("/news", status_code=303)


# -- Trading --------------------------------------------------------------------------


def _position_dict(p) -> dict:
    return {
        "id": p.id,
        "market": p.market,
        "status": p.status,
        "amount": p.amount,
        "entry_price": p.entry_price,
        "entry_quote": p.entry_quote,
        "opened_at": p.opened_at,
        "stop_price": p.stop_price,
        "take_profit_price": p.take_profit_price,
        "exit_price": p.exit_price,
        "closed_at": p.closed_at,
        "pnl_eur": p.pnl_eur,
        "pnl_pct": p.pnl_pct,
        "close_reason": p.close_reason,
    }


def _decision_dict(d) -> dict:
    return {
        "id": d.id,
        "created_at": d.created_at,
        "market": d.market,
        "price": d.price,
        "signal": d.signal,
        "news_label": d.news_label,
        "news_score": d.news_score,
        "source": d.source,
        "action": d.action,
        "confidence": d.confidence,
        "amount_quote": d.amount_quote,
        "executed": d.executed,
        "blocked_by": d.blocked_by,
        "reasoning": d.reasoning,
        "price_1h": d.price_1h,
        "price_24h": d.price_24h,
        "price_72h": d.price_72h,
    }


def _trading_status(engine: TradingEngine, session: Session, settings: Settings) -> dict:
    return {
        "mode": engine.mode,
        "paused": risk.is_paused(session),
        "enabled": settings.trading_enabled,
        "interval_min": settings.trade_interval_min,
        "limits": engine.limits.__dict__,
        "fee_pct": engine.fee_pct,
        "watch": engine.watch_status(),
        "watch_interval_min": settings.watch_interval_min,
        "watch_max_min": settings.watch_max_min,
        "last_watch_run": engine.last_watch_run,
        "trends": load_trends(session, list(engine.markets)) if engine.trend_context else [],
        "events": engine.events_status(session),
        "claude_available": engine.decision_maker.available,
        "last_run": engine.last_run,
        "last_results": engine.last_results,
        "last_error": engine.last_error,
        "trades_today": risk.trades_today(session, engine.mode),
        "realized_pnl_today": round(risk.realized_pnl_today(session, engine.mode), 2),
        "performance_30d": performance_summary(session, engine.mode),
        "exit_interval_min": settings.exit_check_interval_min,
        "last_exit_run": engine.last_exit_run,
        # Open positions the exit loop could not protect (their mode cannot trade now).
        "unprotected_positions": engine.unprotected,
        "live_capital_eur": engine.live_capital_eur,
    }


def _health(
    request: Request,
    engine: TradingEngine,
    session: Session,
    *,
    figures: dict | None = None,
    paused: bool | None = None,
    strategy_invalid: bool | None = None,
) -> dict:
    """bot_health() with what the app knows: Claude budget, the scheduler's last start."""
    if strategy_invalid is None:
        strategy_invalid = load_strategy_checked(session)[1] is not None
    return bot_health(
        engine,
        session,
        request.app.state.settings,
        paused=paused,
        budget=request.app.state.budget.status(session),
        scheduled_last=_load_last_run("trading cycle"),
        risk_figures=figures,
        strategy_invalid=strategy_invalid,
    )


@app.get("/api/status")
async def api_status(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """A light status for pages that refresh themselves: the bot's health, the limits as they
    stand and the unprotected positions. No Bitvavo calls (fast, also while Bitvavo is down);
    /health stays the bare liveness check for Docker and the proxy."""
    figures = risk_now(engine, session, request.app.state.settings)
    body = {
        "mode": engine.mode,
        "health": _health(request, engine, session, figures=figures),
        "risk_now": figures,
        "unprotected_positions": engine.unprotected,
        "now": datetime.now(UTC),
    }
    return JSONResponse(jsonable_encoder(body), headers={"Cache-Control": "no-store"})


@app.get("/api/trading/status")
async def trading_status(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    return _trading_status(engine, session, request.app.state.settings)


@app.post("/api/trading/cycle")
async def trading_cycle(
    engine: TradingEngine = Depends(get_engine), session: Session = Depends(get_session)
):
    try:
        return [r.as_dict() for r in await engine.run_cycle(session)]
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc


class PauseIn(BaseModel):
    paused: bool


@app.post("/api/trading/pause")
async def trading_pause(body: PauseIn, session: Session = Depends(get_session)):
    risk.set_paused(session, body.paused)
    return {"paused": body.paused}


@app.get("/api/positions")
async def positions(
    engine: TradingEngine = Depends(get_engine), session: Session = Depends(get_session)
):
    return {
        "open": [_position_dict(p) for p in open_positions(session, engine.mode)],
        "closed": [_position_dict(p) for p in closed_positions(session, engine.mode)],
    }


@app.get("/api/decisions")
async def decisions(
    limit: int = Query(50, ge=1, le=500),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    return [_decision_dict(d) for d in recent_decisions(session, engine.mode, limit)]


@app.post("/api/evaluations", status_code=201)
async def run_evaluation(
    days: int = Query(7, ge=1, le=365),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    if not engine.evaluator.available:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured")
    try:
        ev = await engine.run_evaluation(session, days)
    except EvaluationBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _evaluation_dict(ev)


def _evaluation_dict(ev: Evaluation) -> dict:
    return {
        "id": ev.id,
        "created_at": ev.created_at,
        "model": ev.model,
        "period_days": ev.period_days,
        "report_nl": ev.report_nl,
        "proposed_strategy": json.loads(ev.proposed_strategy) if ev.proposed_strategy else None,
        "proposal_backtest": json.loads(ev.proposal_backtest) if ev.proposal_backtest else None,
        "applied": ev.applied,
    }


@app.get("/api/evaluations")
async def list_evaluations(session: Session = Depends(get_session)):
    rows = session.scalars(select(Evaluation).order_by(Evaluation.id.desc()).limit(20)).all()
    return [_evaluation_dict(e) for e in rows]


class ProposalError(Exception):
    """An evaluation's proposal cannot be applied; ``status`` and a Dutch ``message``."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _proposal(ev: Evaluation) -> Strategy | None:
    """The proposed strategy of an evaluation, or None when there is none or it is invalid."""
    if not ev.proposed_strategy:
        return None
    try:
        return Strategy.model_validate_json(ev.proposed_strategy)
    except ValidationError:
        return None


# A proposal whose hard buy conditions fire on more candles than this (live, per its stored
# backtest) is not applied with one click: it buys on Claude's judgement almost every cycle.
PROPOSAL_MAX_LIVE_BUY_PCT = 50.0


def _fmt_pct(value: float | None) -> str:
    return "geen stop" if value is None else f"{value:g}%"


def _proposal_guard_problems(current: Strategy, proposal: Strategy, ev: Evaluation) -> list[str]:
    """What an evaluator proposal (which news can influence) may not do on its own: widen or
    drop the stop from entry, or buy on nearly every candle. The owner can still save such a
    strategy deliberately on the strategy page."""
    problems: list[str] = []
    before, after = current.entry_stop_pct(), proposal.entry_stop_pct()
    if before is not None and (after is None or after > before + 1e-9):
        problems.append(
            f"het voorstel maakt de stop ruimer ({_fmt_pct(before)} -> {_fmt_pct(after)}); pas "
            "dat zelf aan op de strategiepagina als je dat wilt"
        )
    try:
        backtest_ = json.loads(ev.proposal_backtest) if ev.proposal_backtest else None
        live_pct = backtest_["proposed"]["live_buy_signal_pct"]
    except (ValueError, KeyError, TypeError):
        live_pct = None
    if isinstance(live_pct, int | float) and live_pct > PROPOSAL_MAX_LIVE_BUY_PCT:
        problems.append(
            f"het voorstel geeft live op {live_pct:g}% van de candles een koopsignaal (meer dan "
            f"{PROPOSAL_MAX_LIVE_BUY_PCT:g}%); pas dat zelf aan op de strategiepagina als je "
            "dat wilt"
        )
    return problems


def _apply_proposal(request: Request, session: Session, evaluation_id: int) -> Strategy:
    """Make an evaluation's proposal the active strategy, with the same checks as a save via
    the API or the strategy page. Raises ProposalError."""
    ev = session.get(Evaluation, evaluation_id)
    if ev is None or not ev.proposed_strategy:
        raise ProposalError(404, "Evaluatie of voorstel niet gevonden.")
    strategy = _proposal(ev)
    if strategy is None:
        raise ProposalError(
            400, "Het voorstel is geen geldige strategie en kan niet worden toegepast."
        )
    if strategy.market not in request.app.state.settings.markets:
        raise ProposalError(400, f"{strategy.market} staat niet in MARKETS.")
    try:
        check_for_use(strategy)
    except ValueError as exc:
        raise ProposalError(400, str(exc)) from exc
    guard = _proposal_guard_problems(load_strategy(session), strategy, ev)
    if guard:
        message = "; ".join(guard)
        raise ProposalError(400, message[0].upper() + message[1:] + ".")
    save_strategy(session, strategy)
    ev.applied = True
    session.commit()
    return strategy


@app.post("/api/evaluations/{evaluation_id}/apply")
async def apply_evaluation(
    request: Request,
    evaluation_id: int = Path(ge=1, le=DB_ID_MAX),
    session: Session = Depends(get_session),
):
    try:
        strategy = _apply_proposal(request, session, evaluation_id)
    except ProposalError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"applied": True, "strategy": strategy.model_dump()}


def _trading_context(
    request: Request, engine: TradingEngine, session: Session, error: str | None = None
) -> dict:
    settings: Settings = request.app.state.settings
    evaluations = session.scalars(select(Evaluation).order_by(Evaluation.id.desc()).limit(5)).all()
    strategy = load_strategy(session)
    last_cycle = latest_decisions(session, engine.mode, list(engine.markets))
    evaluation_rows = []
    for e in evaluations:
        row = _evaluation_dict(e)
        proposal = _proposal(e)
        # What applying would change, and why it cannot be applied (shown on the card).
        row["diff"] = strategy_diff(strategy, proposal) if proposal is not None else []
        if e.proposed_strategy and proposal is None:
            row["problems"] = ["het voorstel is geen geldige strategie"]
        elif proposal is not None:
            row["problems"] = use_problems(proposal)
            if proposal.market not in settings.markets:
                row["problems"].append(f"{proposal.market} staat niet in MARKETS")
            row["problems"] += _proposal_guard_problems(strategy, proposal, e)
        else:
            row["problems"] = []
        evaluation_rows.append(row)
    status = _trading_status(engine, session, settings)
    figures = risk_now(engine, session, settings)
    return {
        "settings": settings,
        "status": status,
        # One status for the hero (app/trading/health.py) and the limits as current figures.
        "health": _health(request, engine, session, figures=figures, paused=status["paused"]),
        "risk_now": figures,
        "open_positions": open_positions(session, engine.mode),
        "closed_positions": closed_positions(session, engine.mode)[:20],
        "decisions": recent_decisions(
            session, engine.mode, limit=5000, days=settings.decision_history_days
        ),
        "last_cycle": last_cycle,
        "levels": {d.id: levels_summary(strategy, d.indicator_values()) for d in last_cycle},
        "evaluations": evaluation_rows,
        "error": error,
    }


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request, "trading.html", _trading_context(request, engine, session)
    )


APPLY_ACTION_RE = re.compile(r"apply:([0-9]{1,9})")


@app.post("/trading/action")
async def trading_action(
    request: Request,
    action: str = Form(..., max_length=32),
    next_url: str = Form("", alias="next", max_length=512),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """Kill switch, cycle, evaluation and proposals. ``next`` (from the kill-switch macro) is
    the page to return to; only paths on this site are followed."""
    if action == "pause":
        risk.set_paused(session, True)
    elif action == "resume":
        risk.set_paused(session, False)
    elif action == "cycle":
        await engine.run_cycle(session)
    elif action == "evaluate" and engine.evaluator.available:
        try:
            await engine.run_evaluation(session)
        except EvaluationBusy as exc:
            ctx = _trading_context(request, engine, session, error=str(exc))
            return templates.TemplateResponse(request, "trading.html", ctx, status_code=409)
    elif action.startswith("apply:"):
        match = APPLY_ACTION_RE.fullmatch(action)
        if match is None:
            raise HTTPException(400, "Ongeldig voorstel: verwacht apply:<nummer>.")
        try:
            _apply_proposal(request, session, int(match.group(1)))
        except ProposalError as exc:
            ctx = _trading_context(request, engine, session, error=exc.message)
            return templates.TemplateResponse(request, "trading.html", ctx, status_code=exc.status)
    return RedirectResponse(_safe_next(next_url) if next_url else "/trading", status_code=303)


# -- Trading plan ---------------------------------------------------------------------


def _plan_context(request: Request, engine: TradingEngine, session: Session) -> dict:
    """Everything the plan page shows: the owner's own text plus the rules the code follows."""
    settings: Settings = request.app.state.settings
    strategy = load_strategy(session)
    return {
        "settings": settings,
        "plan": load_plan(session),
        "fields": PLAN_FIELDS,
        "max_length": PLAN_MAX_LENGTH,
        "strategy": strategy,
        "mode": engine.mode,
        "limits": engine.limits,
        "fee_pct": engine.fee_pct,
        "news_entry_min_score": NEWS_ENTRY_MIN_SCORE,
        "news_exit_max_score": NEWS_EXIT_MAX_SCORE,
        "half_order_pct": int(NEWS_ENTRY_SIZE_FACTOR * 100),
        "budget": request.app.state.budget.status(session),
        "paused": risk.is_paused(session),
        "claude_available": engine.decision_maker.available,
        "error": None,
        "message": None,
    }


@app.get("/api/plan")
async def api_plan(session: Session = Depends(get_session)):
    return load_plan(session).model_dump()


@app.put("/api/plan")
async def api_put_plan(plan: TradingPlan, session: Session = Depends(get_session)):
    return save_plan(session, plan).model_dump()


PLAN_SAVED = "Tradingplan opgeslagen. Claude krijgt het vanaf de volgende beslissing mee."


@app.get("/plan", response_class=HTMLResponse)
async def plan_page(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    ctx = _plan_context(request, engine, session)
    return templates.TemplateResponse(request, "plan.html", ctx)


@app.post("/plan", response_class=HTMLResponse)
async def plan_save(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    form = await request.form()
    raw = {key: str(form.get(key) or "") for key in PLAN_FIELDS}
    try:
        plan = TradingPlan.model_validate(raw)
    except ValidationError as exc:
        ctx = _plan_context(request, engine, session)
        ctx["plan"] = TradingPlan.model_construct(**raw)
        ctx["error"] = "Niet opgeslagen: " + "; ".join(
            f"{PLAN_FIELDS.get(str(e['loc'][0]), (str(e['loc'][0]),))[0]}: {e['msg']}"
            for e in exc.errors()
        )
        return templates.TemplateResponse(request, "plan.html", ctx, status_code=400)
    save_plan(session, plan)
    # A one-time message (not ?saved=1): a reload or a restored tab shows no stale "saved".
    return flash.redirect_with_flash("/plan", "ok", PLAN_SAVED)


# -- Trade journal --------------------------------------------------------------------


def _journal_filter(
    market: str | None,
    status: str | None,
    result: str | None,
    reason: str | None,
    tag: str | None,
    days: int | None,
    q: str | None,
) -> JournalFilter:
    return JournalFilter(
        market=(market or "").upper() or None,
        status=status if status in ("open", "closed") else None,
        result=result if result in ("win", "loss") else None,
        reason=reason if reason in EXIT_NL else None,
        tag=(tag or "").strip() or None,
        days=days if days and days > 0 else None,
        query=(q or "").strip() or None,
    )


# Journal filters: free text is short, days stay within what timedelta and the database
# handle.
JOURNAL_FILTERS = {
    "market": Query(None, max_length=16),
    "status": Query(None, max_length=16),
    "result": Query(None, max_length=16),
    "reason": Query(None, max_length=32),
    "tag": Query(None, max_length=64),
    "days": Query(None, ge=0, le=3650),
    "q": Query(None, max_length=200),
}


@app.get("/api/journal")
async def api_journal(
    market: str | None = JOURNAL_FILTERS["market"],
    status: str | None = JOURNAL_FILTERS["status"],
    result: str | None = JOURNAL_FILTERS["result"],
    reason: str | None = JOURNAL_FILTERS["reason"],
    tag: str | None = JOURNAL_FILTERS["tag"],
    days: int | None = JOURNAL_FILTERS["days"],
    q: str | None = JOURNAL_FILTERS["q"],
    limit: int = Query(200, ge=1, le=1000),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    flt = _journal_filter(market, status, result, reason, tag, days, q)
    entries = journal_entries(session, engine.mode, flt, limit)
    return {
        "mode": engine.mode,
        "totals": journal_totals(entries),
        "entries": [e.as_dict() for e in entries],
    }


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=4000)
    tags: str = Field(default="", max_length=256)


@app.put("/api/journal/{position_id}/note")
async def api_journal_note(
    body: NoteIn,
    position_id: int = Path(ge=1, le=DB_ID_MAX),
    session: Session = Depends(get_session),
):
    try:
        save_note(session, position_id, body.note, body.tags)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    entry = journal_entry(session, position_id)
    assert entry is not None
    return entry.as_dict()


@app.get("/journal", response_class=HTMLResponse)
async def journal_page(
    request: Request,
    market: str | None = JOURNAL_FILTERS["market"],
    status: str | None = JOURNAL_FILTERS["status"],
    result: str | None = JOURNAL_FILTERS["result"],
    reason: str | None = JOURNAL_FILTERS["reason"],
    tag: str | None = JOURNAL_FILTERS["tag"],
    days: int | None = JOURNAL_FILTERS["days"],
    q: str | None = JOURNAL_FILTERS["q"],
    saved: int | None = Query(None, ge=0, le=DB_ID_MAX),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    flt = _journal_filter(market, status, result, reason, tag, days, q)
    entries = journal_entries(session, engine.mode, flt)
    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "settings": settings,
            "mode": engine.mode,
            "filter": flt,
            "entries": entries,
            "totals": journal_totals(entries),
            "tags": all_tags(session, engine.mode),
            "exit_reasons": EXIT_NL,
            "other_orders": unlinked_orders(session, engine.mode) if not flt.active else [],
            "saved": saved,
            # Where the note form returns to: this page with its filters, minus "saved".
            "back_url": request.url.remove_query_params("saved").path
            + (f"?{request.url.remove_query_params('saved').query}" if request.url.query else ""),
        },
    )


@app.post("/journal/{position_id}/note")
async def journal_note_form(
    request: Request,
    position_id: int = Path(ge=1, le=DB_ID_MAX),
    note: str = Form(""),
    tags: str = Form(""),
    back: str = Form("", max_length=2000),
    session: Session = Depends(get_session),
):
    try:
        save_note(session, position_id, note, tags)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    target = back if back.startswith("/journal") else "/journal"
    sep = "&" if "?" in target else "?"
    url = f"{target}{sep}saved={position_id}#trade-{position_id}"
    return RedirectResponse(url, status_code=303)


# -- Settings -------------------------------------------------------------------------

# How long a settings save may wait for Bitvavo's market list (to refuse a market that does not
# exist); when Bitvavo does not answer in time the save goes on without that check.
MARKET_LIST_TIMEOUT_S = 3.0
MARKET_LIST_TTL_S = 3600.0
MARKET_LIST_RETRY_S = 60.0
BITVAVO_TEST_TIMEOUT_S = 8.0
BITVAVO_TEST_REPEAT_S = 10.0
# Flash texts are cut at 400 characters (app/flash.py); keep a list of changes below that.
CHANGES_TEXT_MAX = 330
# The blocks of the settings page that can show a message after an action (?saved=<block>).
SETTINGS_BLOCKS = (*cfg.GROUPS, cfg.PAPER_GROUP, "omgeving")


def _editable_values(settings: Settings) -> dict:
    return {key: getattr(settings, key) for key in cfg.EDITABLE_KEYS}


def live_requirements(settings: Settings, client: BitvavoClient) -> list[dict]:
    """Every condition for live trading, met or not, with what to do about it. All of them live
    in the server environment on purpose; live_blocker() gives the first one that is missing."""
    return [
        {
            "key": "login",
            "ok": settings.auth_enabled,
            "label": "De login staat aan",
            "missing": "de login staat uit (AUTH_ENABLED=false)",
            "fix": "Zet AUTH_ENABLED=true in de serveromgeving en herstart de app.",
            "env": "AUTH_ENABLED",
        },
        {
            "key": "bitvavo_key",
            "ok": client.authenticated,
            "label": "Er is een Bitvavo-sleutel",
            "missing": "er is geen Bitvavo-sleutel (BITVAVO_API_KEY en BITVAVO_API_SECRET)",
            "fix": "Maak bij Bitvavo een API-sleutel met rechten om te lezen en te handelen, "
            "zonder opnemen, en zet hem in de serveromgeving.",
            "env": "BITVAVO_API_KEY, BITVAVO_API_SECRET",
        },
        {
            "key": "ack",
            "ok": settings.live_trading_ack,
            "label": "Live handelen is op de server bevestigd",
            "missing": "LIVE_TRADING_ACK staat niet op true in de serveromgeving",
            "fix": "Zet LIVE_TRADING_ACK=true in de serveromgeving en herstart de app.",
            "env": "LIVE_TRADING_ACK",
        },
    ]


def _live_missing_text(env: Settings, client: BitvavoClient) -> str | None:
    missing = [r["missing"] for r in live_requirements(env, client) if not r["ok"]]
    return "; ".join(missing) or None


# Bitvavo error codes for a refused key, in plain Dutch (the code itself is shown as detail).
KEY_PROBLEMS_NL = {
    301: "de sleutel heeft niet de goede lengte; controleer BITVAVO_API_KEY",
    302: "de klok van de server loopt niet gelijk",
    304: "de klok van de server loopt niet gelijk",
    305: "Bitvavo kent deze sleutel niet (meer)",
    306: "de sleutel is nog niet per e-mail bevestigd",
    307: "de sleutel mag niet vanaf het IP-adres van deze server",
    308: "de handtekening klopt niet; controleer BITVAVO_API_SECRET",
    309: "de handtekening klopt niet; controleer BITVAVO_API_SECRET",
    310: "de sleutel mag niet handelen",
    311: "de sleutel mag je accountgegevens niet lezen",
    317: "je Bitvavo-account is geblokkeerd",
    318: "je Bitvavo-account moet nog worden geverifieerd",
}


def _key_problem_nl(problem: dict) -> str:
    code = problem.get("code")
    if code in KEY_PROBLEMS_NL:
        return KEY_PROBLEMS_NL[code]
    if problem.get("status") == 401:
        return "Bitvavo vraagt om een geldige sleutel"
    return "Bitvavo weigert de sleutel"


def bitvavo_key_state(request: Request) -> dict:
    """What is known about the Bitvavo key: missing, set (not checked yet), ok, refused
    ("rejected"), or set but without trading rights ("no_trading"). The client notes every
    refusal of a signed request (app/bitvavo/client.py), so a key Bitvavo rejects never shows
    as plain 'ingesteld'. Used on the settings page and in the footer of every page."""
    state = getattr(getattr(request, "app", None), "state", None)
    client = state._state.get("client") if state is not None else None
    if client is None or not getattr(client, "authenticated", False):
        return {"state": "missing", "label": "niet ingesteld", "reason": None, "at": None}

    def refused(problem: dict, kind: str, label: str) -> dict:
        detail = f"Bitvavo {problem.get('status')}"
        if problem.get("code") is not None:
            detail += f", code {problem.get('code')}"
        if problem.get("error"):
            detail += f": {problem['error']}"
        return {
            "state": kind,
            "label": label,
            "reason": _key_problem_nl(problem),
            "detail": detail,
            "at": datetime.fromtimestamp(problem.get("at") or 0, UTC),
        }

    if client.key_problem:
        return refused(client.key_problem, "rejected", "ingesteld, maar geweigerd door Bitvavo")
    if client.trade_problem:
        return refused(client.trade_problem, "no_trading", "ingesteld, maar mag niet handelen")
    if client.key_ok_at:
        at = datetime.fromtimestamp(client.key_ok_at, UTC)
        return {"state": "ok", "label": "ingesteld en werkt", "reason": None, "at": at}
    return {"state": "set", "label": "ingesteld", "reason": None, "at": None}


templates.env.globals["bitvavo_key_state"] = bitvavo_key_state


async def fetch_market_names(client: BitvavoClient) -> set[str] | None:
    """Every market Bitvavo lists, or None when the list cannot be fetched quickly."""
    try:
        rows = await asyncio.wait_for(client.markets(), MARKET_LIST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - offline or slow: the save goes on unchecked
        log.info("market list not available for the settings check: %s", exc)
        return None
    if not isinstance(rows, list):
        return None
    names = {r["market"] for r in rows if isinstance(r, dict) and isinstance(r.get("market"), str)}
    return names or None


async def _known_markets(app_: FastAPI) -> set[str] | None:
    """Bitvavo's market list, kept for an hour (a failed fetch is retried after a minute)."""
    cached = getattr(app_.state, "market_names", None)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    names = await fetch_market_names(app_.state.client)
    app_.state.market_names = (now + (MARKET_LIST_TTL_S if names else MARKET_LIST_RETRY_S), names)
    return names


def _settings_payload(request: Request, session: Session) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "mode": settings.trading_mode,
        "live_blocker": live_blocker(request.app.state.env, request.app.state.client),
        "warning": request.app.state.config_warning,
        "values": _editable_values(settings),
        "groups": cfg.describe(session, settings, request.app.state.env),
        "warnings": {
            key: text
            for key in cfg.EDITABLE_KEYS
            if cfg.SPEC_BY_KEY[key].kind == "model"
            and (text := cfg.model_warning(getattr(settings, key)))
        },
    }


async def _check_settings(request: Request, session: Session, raw: dict) -> tuple[dict, Settings]:
    """Parse and validate ``raw`` into (values, the settings they would give). Raises
    SettingsError with Dutch messages per field; nothing is stored."""
    app_ = request.app
    values = cfg.parse_values(raw)
    candidate = cfg.preview(session, values, app_.state.env)
    if candidate.trading_mode == "live" and app_.state.broker.mode != "live":
        missing = _live_missing_text(app_.state.env, app_.state.client)
        if missing:
            raise SettingsError({"trading_mode": f"live handelen kan nog niet: {missing}"})
    new_markets = [m for m in values.get("markets", []) if m not in app_.state.settings.markets]
    if new_markets:
        known = await _known_markets(app_)
        problem = cfg.unknown_markets(new_markets, known) if known else None
        if problem:
            raise SettingsError({"markets": problem})
    if "trading_fee_pct" not in values:
        # Keep the account's real Bitvavo fee found at startup; a save of another setting
        # must not swap it back for TRADING_FEE_PCT.
        candidate = candidate.model_copy(
            update={"trading_fee_pct": app_.state.settings.trading_fee_pct}
        )
    return values, candidate


async def _commit_settings(
    request: Request, session: Session, values: dict, candidate: Settings, password: str | None
) -> Settings:
    """Store and apply checked values; raises StepUpError when a sensitive setting changes
    without the right current password."""
    app_ = request.app
    if _sensitive_changes(session, app_.state, candidate):
        await require_current_password(request, session, password)
    cfg.save_overrides(session, values, app_.state.env)
    _log_setting_changes(app_.state.settings, candidate)
    apply_settings(app_, candidate)
    _drop_disabled_mood(session, candidate)
    app_.state.config_warning = None
    return candidate


async def _save_settings(
    request: Request, session: Session, raw: dict, password: str | None = None
) -> Settings:
    """Parse, validate, store and apply; raises SettingsError with per-field messages, or
    StepUpError when a sensitive setting changes without the right current password."""
    values, candidate = await _check_settings(request, session, raw)
    return await _commit_settings(request, session, values, candidate, password)


def _sensitive_changes(session: Session, state, candidate: Settings) -> list[str]:
    """Sensitive settings that ``candidate`` really changes.

    A key counts only when it differs from the running settings *and* from the stored ones,
    so a runtime-only override (the real Bitvavo fee found at startup, or the paper fallback
    of trading_mode) does not make every save look sensitive.
    """
    runtime: Settings = state.settings
    stored = cfg.effective_settings(session, state.env).model_copy(
        update={"trading_mode": runtime.trading_mode}
    )
    return [
        k
        for k in cfg.sensitive_changes(runtime, candidate)
        if k in cfg.sensitive_changes(stored, candidate)
    ]


def _drop_disabled_mood(session: Session, settings: Settings) -> None:
    """With the Fear & Greed feed off, Claude must not keep getting its last stored value."""
    if not settings.fear_greed_enabled:
        clear_market_mood(session)


def _log_setting_changes(old: Settings, new: Settings) -> None:
    """An audit trail in the container log: every portal change with old and new value."""
    for key in cfg.EDITABLE_KEYS:
        before, after = getattr(old, key), getattr(new, key)
        if before != after:
            log.info("setting %s changed in the portal: %r -> %r", key, before, after)


def _resets_sensitive(session: Session, keys: list[str] | None) -> bool:
    """Whether forgetting ``keys`` (None: all) removes a stored value of a sensitive setting."""
    stored = cfg.load_overrides(session)
    return any(k in cfg.SENSITIVE_KEYS and (keys is None or k in keys) for k in stored)


RESET_TO_LIVE = (
    "Niet teruggezet: de serveromgeving staat op TRADING_MODE=live, dus terugzetten zou de bot "
    "naar Live (echt geld) zetten. Kies de modus zelf bij Handelen › Modus."
)


async def _reset_settings(
    request: Request, session: Session, keys: list[str] | None, password: str | None = None
) -> tuple[int, list[str]]:
    """Forget stored values; returns (how many, the changes as 'Label: oud → nieuw').

    A reset never switches the bot to live: when the environment says live (and live is
    possible) while the portal holds paper mode, the reset is refused (SettingsError); the
    mode then only changes through its own choice with the live confirmation.
    """
    old: Settings = request.app.state.settings
    env: Settings = request.app.state.env
    new = cfg.preview_reset(session, keys, env)
    if (
        new.trading_mode == "live"
        and request.app.state.broker.mode != "live"
        and not live_blocker(env, request.app.state.client)
    ):
        raise SettingsError({"trading_mode": RESET_TO_LIVE})
    if _resets_sensitive(session, keys):
        await require_current_password(request, session, password)
    removed = cfg.reset_overrides(session, keys)
    new = cfg.effective_settings(session, env)
    if keys is not None and "trading_fee_pct" not in keys:
        # Keep the account's real Bitvavo fee found at startup (see _check_settings).
        new = new.model_copy(update={"trading_fee_pct": old.trading_fee_pct})
    if new.trading_mode == "live" and live_blocker(env, request.app.state.client):
        # The environment itself says live but live is not possible: stay in paper mode.
        new = new.model_copy(update={"trading_mode": "paper"})
    _log_setting_changes(old, new)
    apply_settings(request.app, new)
    _drop_disabled_mood(session, new)
    return removed, cfg.changes(old, new, keys)


async def _api_password(request: Request, body: dict | None = None) -> str | None:
    """The current password of an API call: the JSON field current_password, or the
    X-Current-Password header (handy for DELETE, which usually has no body)."""
    if body is None:
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                raise HTTPException(400, "ongeldige JSON") from None
    value = body.get("current_password") if isinstance(body, dict) else None
    if value is not None and not isinstance(value, str):
        raise HTTPException(422, {"current_password": "moet tekst zijn"})
    return value or request.headers.get(PASSWORD_HEADER)


@app.get("/api/settings")
async def api_settings(request: Request, session: Session = Depends(get_session)):
    return _settings_payload(request, session)


@app.put("/api/settings")
async def api_update_settings(
    body: dict, request: Request, session: Session = Depends(get_session)
):
    """Changes to sensitive settings need ``current_password`` in the body (step-up)."""
    password = await _api_password(request, body)
    values = {k: v for k, v in body.items() if k != "current_password"}
    try:
        await _save_settings(request, session, values, password)
    except SettingsError as exc:
        raise HTTPException(422, exc.errors) from exc
    except StepUpError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return _settings_payload(request, session)


@app.delete("/api/settings")
async def api_reset_settings(request: Request, session: Session = Depends(get_session)):
    """Forget every value saved in the portal; the environment applies again."""
    password = await _api_password(request)
    try:
        await _reset_settings(request, session, None, password)
    except SettingsError as exc:
        raise HTTPException(409, exc.errors) from exc
    except StepUpError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return _settings_payload(request, session)


@app.delete("/api/settings/{key}")
async def api_reset_setting(key: str, request: Request, session: Session = Depends(get_session)):
    if key not in cfg.SPEC_BY_KEY:
        raise HTTPException(404, f"{key} is geen aanpasbare instelling")
    password = await _api_password(request)
    try:
        await _reset_settings(request, session, [key], password)
    except SettingsError as exc:
        raise HTTPException(409, exc.errors) from exc
    except StepUpError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return _settings_payload(request, session)


def _reset_paper_portfolio(session: Session) -> int:
    """Forget the paper wallet and close open paper positions without result."""
    row = session.get(Setting, PAPER_WALLET_KEY)
    if row is not None:
        session.delete(row)
    closed = 0
    for p in open_positions(session, "paper"):
        p.status = "closed"
        p.exit_price = p.entry_price
        p.exit_quote = p.entry_quote
        p.closed_at = datetime.now(UTC)
        p.pnl_eur = 0.0
        p.pnl_pct = 0.0
        p.close_reason = "reset"
        closed += 1
    session.commit()
    return closed


def _join_changes(changes: list[str]) -> str:
    """'A: 1 → 2; B: aan → uit', cut to fit a flash, with 'en nog 3 wijzigingen'."""
    shown: list[str] = []
    for i, text in enumerate(changes):
        if shown and len("; ".join([*shown, text])) > CHANGES_TEXT_MAX:
            rest = len(changes) - i
            return "; ".join(shown) + f"; en nog {rest} wijziging{'' if rest == 1 else 'en'}"
        shown.append(text)
    return "; ".join(shown)


def _reset_confirm_text(group: dict) -> str:
    """The question before 'Standaard herstellen' of a block: exactly what goes back."""
    lines = [
        f"• {f['label']}: {f['display']} → {f['default_display']}"
        for f in group["fields"]
        if f["overridden"] and f["key"] != "trading_mode"
    ]
    text = f"Deze instellingen in '{group['title']}' gaan terug naar de standaard uit de " + (
        "serveromgeving:\n" + "\n".join(lines)
    )
    if any(f["key"] == "trading_mode" and f["overridden"] for f in group["fields"]):
        text += "\n\nDe modus verandert niet; die kies je alleen zelf."
    return text


def _live_context(request: Request, settings: Settings, key_state: dict) -> dict:
    """The checklist and confirmation panel for switching to live."""
    env: Settings = request.app.state.env
    requirements = live_requirements(env, request.app.state.client)
    capital = settings.live_capital_eur
    advice = [
        {
            "ok": capital > 0,
            "label": f"Kapitaal voor de bot: {eur(capital, 0)}"
            if capital > 0
            else "Kapitaal voor de bot staat op 0: de limieten rekenen dan met je hele "
            "Bitvavo-account",
            "fix": "Stel een bedrag in bij Harde limieten › Kapitaal voor de bot (live).",
            "href": "#f-live_capital_eur",
            "link": "Kapitaal instellen",
        },
        {
            "ok": key_state["state"] == "ok",
            "label": {
                "ok": "De Bitvavo-sleutel werkt",
                "rejected": "Bitvavo weigert de sleutel: " + str(key_state.get("reason")),
                "no_trading": "De Bitvavo-sleutel mag niet handelen",
                "set": "De Bitvavo-sleutel is nog niet getest",
                "missing": "Er is nog geen Bitvavo-sleutel om te testen",
            }[key_state["state"]],
            "fix": "Test de verbinding onder 'Uit de omgeving'. Geef de sleutel geen rechten "
            "om geld op te nemen; Crypie kan dat zelf niet controleren.",
            "href": "#omgeving",
            "link": "Verbinding testen",
        },
        {
            "ok": notify_configured(env),
            "label": "Meldingen buiten de app staan aan"
            if notify_configured(env)
            else "Er is geen meldingskanaal: bij een storing hoor je niets buiten de app",
            "fix": "Zet NOTIFY_URL in de serveromgeving (bijvoorbeeld een ntfy-topic).",
            "href": "#omgeving",
            "link": "Meldingen bekijken",
        },
    ]
    return {
        "requirements": requirements,
        "missing": [r for r in requirements if not r["ok"]],
        "advice": advice,
        "can_switch": settings.trading_mode != "live" and all(r["ok"] for r in requirements),
        "summary": [
            ("Maximum per order", eur(settings.max_trade_eur, 0)),
            (
                "Kapitaal voor de bot",
                eur(capital, 0) if capital > 0 else "0: het hele Bitvavo-account",
            ),
            ("Maximaal in crypto", f"{num(settings.max_exposure_pct, 0)}%"),
            ("Dagverlieslimiet", f"{num(settings.daily_loss_limit_pct, 1)}%"),
            ("Maximum aantal orders per dag", str(settings.max_trades_per_day)),
            ("Markten", ", ".join(settings.markets)),
            ("Automatisch handelen", "aan" if settings.trading_enabled else "uit"),
        ],
        "capital_zero": capital <= 0,
    }


# Form fields that are no setting: the typed live confirmation (input id "confirm_live").
EXTRA_FORM_FIELDS = {"confirm_live": "Bevestiging voor Live"}


def _error_label(key: str, fields: dict[str, dict]) -> str:
    if key in fields:
        return fields[key]["label"]
    return EXTRA_FORM_FIELDS.get(key, key)


def _settings_context(
    request: Request,
    session: Session,
    errors: dict[str, str] | None = None,
    submitted: dict | None = None,
    message: str | None = None,
    password_error: tuple[str, str] | None = None,
    *,
    form_group: str | None = None,
    password_status: int | None = None,
    block_flashes: list[dict] | None = None,
    saved_block: str | None = None,
    loosened: list[str] | None = None,
    form_extra: dict | None = None,
) -> dict:
    """``password_error`` is (form, message): the form ("trading", ..., or "paper") whose
    current-password field gets the message. ``form_group`` is the block that was submitted
    (its fields show what was sent and which of them are not saved), ``block_flashes`` the
    messages for the block named by ``saved_block``."""
    payload = _settings_payload(request, session)
    settings: Settings = request.app.state.settings
    env: Settings = request.app.state.env
    errors = errors or {}
    paper_field = cfg.describe_field(session, settings, "paper_start_eur", env)
    fields_by_key = {f["key"]: f for g in payload["groups"] for f in g["fields"]}
    fields_by_key[paper_field["key"]] = paper_field
    for f in fields_by_key.values():
        f["changed"] = False
        f["error"] = errors.get(f["key"])
    if submitted:
        for key, sent in submitted.items():
            f = fields_by_key.get(key)
            if f is None:
                continue
            f["text"] = sent
            if f["kind"] == "bool":
                f["value"] = str(sent).lower() in ("true", "1", "on")
                sent_form = "true" if f["value"] else "false"
            elif f["kind"] in ("choice", "model"):
                f["value"] = sent
                sent_form = str(sent)
            else:
                sent_form = str(sent)
            f["changed"] = sent_form.strip() != str(f["saved_text"]).strip()
    # Fields that only work while their switch is on (or their number above 0) are dimmed with
    # an explanation while it is off; they keep their value and are still saved.
    for f in fields_by_key.values():
        parent = fields_by_key.get(f["depends"]) if f["depends"] else None
        f["dep_off"] = False
        if parent is not None and parent["kind"] == "bool":
            f["dep_off"] = not parent["value"]
        elif parent is not None:
            try:
                f["dep_off"] = cfg.parse_decimal(parent["text"]) == 0
            except ValueError:
                pass
    plan = fields_by_key.get("cryptopanic_plan")
    if plan is not None and not env.cryptopanic_token:
        plan["dep_off"] = True
        plan["depends_text"] = (
            "Geldt alleen als er een CryptoPanic-token in de serveromgeving staat; nu is er geen."
        )
    for group in payload["groups"]:
        group["changed"] = [f for f in group["fields"] if f["overridden"]]
        group["resettable"] = [f for f in group["changed"] if f["key"] != "trading_mode"]
        group["reset_needs_password"] = any(f["sensitive"] for f in group["resettable"])
        group["unsaved"] = [f for f in group["fields"] if f["changed"]]
        # The error summary at the top of the submitted block: one link per field.
        group["errors"] = (
            [
                {"key": key, "label": _error_label(key, fields_by_key)}
                for key in errors
                if key in {f["key"] for f in group["fields"]} or key in EXTRA_FORM_FIELDS
            ]
            if group["key"] == form_group
            else []
        )
        group["reset_confirm"] = _reset_confirm_text(group)
    key_state = bitvavo_key_state(request)
    attempts_left = None
    if password_error and password_status == 403 and step_up_enabled(request):
        attempts_left = step_up_attempts_left(request, session)
    return {
        "settings": settings,
        "groups": payload["groups"],
        "paper_field": paper_field,
        "live_blocker": payload["live_blocker"],
        "live": _live_context(request, settings, key_state),
        "config_warning": payload["warning"],
        "errors": errors,
        "message": message,
        "password_error": password_error,
        "password_throttled": password_status == 429,
        "attempts_left": attempts_left,
        "form_group": form_group,
        "form_extra": form_extra or {},
        "loosened": loosened or [],
        "block_flashes": block_flashes or [],
        "saved_block": saved_block,
        "step_up": step_up_enabled(request),
        "sensitive_groups": [g for g in cfg.GROUPS if cfg.group_has_sensitive(g)],
        "model_choices": [m for m, _ in cfg.MODEL_CHOICES],
        "key_state": key_state,
        "environment": {
            "bitvavo_key": env.has_bitvavo_credentials,
            "anthropic_key": bool(env.anthropic_api_key),
            "cryptopanic_token": bool(env.cryptopanic_token),
            "live_ack": env.live_trading_ack,
            "auth": env.auth_enabled,
            "database": env.database_url.split(":", 1)[0],
            "notify": notify_configured(env),
            "notify_format": env.notify_format,
        },
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, saved: str | None = None, session: Session = Depends(get_session)
):
    # A message after an action (a flash) is shown in the block it is about, where the browser
    # lands (#block), instead of at the top of the page. Without the flash (a reload later on)
    # ?saved= shows nothing: it never confirms something twice.
    block_flashes: list[dict] = []
    if saved in SETTINGS_BLOCKS:
        block_flashes = list(getattr(request.state, "flashes", []) or [])
        request.state.flashes = []
    ctx = _settings_context(
        request,
        session,
        block_flashes=block_flashes,
        saved_block=saved if block_flashes else None,
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


def _saved_text(title: str, changes: list[str]) -> str:
    if not changes:
        return f"{title} opgeslagen. Er was niets gewijzigd."
    return f"{title} opgeslagen; het geldt direct. {_join_changes(changes)}."


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    group = str(form.get("group") or "")
    action = str(form.get("action") or "save")
    password = str(form.get("current_password") or "")

    def page(status: int, **kw):
        ctx = _settings_context(request, session, **kw)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=status)

    def step_up_failed(form_key: str, exc: StepUpError, submitted: dict | None = None):
        return page(
            exc.status,
            submitted=submitted,
            password_error=(form_key, exc.message),
            password_status=exc.status,
            form_group=form_key,
        )

    if action == "reset_paper":
        return await _reset_paper_action(request, session, form, password, page, step_up_failed)
    if group not in cfg.GROUPS:
        raise HTTPException(400, "onbekende groep")
    title = cfg.GROUPS[group][0]
    if action == "reset":
        # The mode is never part of a block reset: it changes only through its own choice.
        keys = [k for k in cfg.keys_in_group(group) if k != "trading_mode"]
        try:
            _, changed = await _reset_settings(request, session, keys, password)
        except StepUpError as exc:
            return step_up_failed(group, exc)
        except SettingsError as exc:
            return page(409, errors=exc.errors, form_group=group)
        text = (
            f"Standaardwaarden hersteld in {title}: {_join_changes(changed)}."
            if changed
            else f"In {title} stond alles al op de standaard."
        )
        return flash.redirect_with_flash(f"/settings?saved={group}#{group}", "ok", text)
    raw: dict[str, str] = {}
    for key in cfg.keys_in_group(group):
        sent = form.getlist(key)
        if sent:
            raw[key] = str(sent[-1])  # a checkbox posts after its hidden "false" twin
        elif cfg.SPEC_BY_KEY[key].kind == "bool":
            raw[key] = "false"
    extra = {"confirm_live": str(form.get("confirm_live") or "")[:20]}
    old: Settings = request.app.state.settings
    try:
        values, candidate = await _check_settings(request, session, raw)
        if candidate.trading_mode == "live" and request.app.state.broker.mode != "live":
            # Switching to real money in the portal: the live confirmation is typed, not clicked.
            if extra["confirm_live"].strip().upper() != "LIVE":
                raise SettingsError(
                    {
                        "confirm_live": "Typ LIVE in het vak onder de modus om te bevestigen dat "
                        "de bot met echt geld gaat handelen."
                    }
                )
        loosened = cfg.loosened_limits(old, candidate)
        if loosened and str(form.get("confirm_loosen") or "") != "1":
            # A hard limit more than twice as loose in one go: ask once more (before the
            # password, so nothing is checked or counted yet).
            return page(409, submitted=raw, form_group=group, loosened=loosened)
        await _commit_settings(request, session, values, candidate, password)
    except SettingsError as exc:
        return page(400, errors=exc.errors, submitted=raw, form_group=group, form_extra=extra)
    except StepUpError as exc:
        return step_up_failed(group, exc, submitted=raw)
    response = flash.redirect_with_flash(
        f"/settings?saved={group}#{group}",
        "ok",
        _saved_text(title, cfg.changes(old, candidate, cfg.keys_in_group(group))),
    )
    for key in cfg.keys_in_group(group):
        warning = cfg.model_warning(getattr(candidate, key)) if key in values else None
        if warning and cfg.SPEC_BY_KEY[key].kind == "model":
            flash.add_flash(
                response,
                "warn",
                f"{cfg.SPEC_BY_KEY[key].label} ({getattr(candidate, key)}): " + warning,
            )
    return response


async def _reset_paper_action(request, session, form, password, page, step_up_failed):
    """Reset the paper portfolio, optionally with a new start amount (saved with it)."""
    if request.app.state.broker.mode != "paper":
        raise HTTPException(400, "alleen mogelijk in oefenmodus")
    sent = form.get("paper_start_eur")
    submitted = {"paper_start_eur": str(sent)} if sent is not None else None
    values: dict = {}
    if sent is not None and str(sent).strip():
        try:
            values = cfg.parse_values({"paper_start_eur": str(sent)})
            cfg.preview(session, values, request.app.state.env)
        except SettingsError as exc:
            return page(400, errors=exc.errors, submitted=submitted, form_group=cfg.PAPER_GROUP)
    try:
        await require_current_password(request, session, password)
    except StepUpError as exc:
        return step_up_failed("paper", exc, submitted)
    current: Settings = request.app.state.settings
    if values and values["paper_start_eur"] != current.paper_start_eur:
        cfg.save_overrides(session, values, request.app.state.env)
        new = current.model_copy(update=values)
        _log_setting_changes(current, new)
        apply_settings(request.app, new)
    closed = _reset_paper_portfolio(session)
    log.info("paper portfolio reset in the portal")
    start = request.app.state.settings.paper_start_eur
    positions = (
        f" {closed} open oefenpositie{'' if closed == 1 else 's'} gesloten zonder resultaat."
        if closed
        else ""
    )
    return flash.redirect_with_flash(
        "/settings?saved=paper",
        "ok",
        f"Oefenportefeuille gereset. Het saldo staat weer op {eur(start, 0)}.{positions}",
    )


_last_bitvavo_test = 0.0


@app.post("/settings/bitvavo-test")
async def settings_bitvavo_test(request: Request):
    """'Verbinding testen': one signed request (account and fee) with the configured key."""
    global _last_bitvavo_test
    target = "/settings?saved=omgeving#omgeving"
    client: BitvavoClient = request.app.state.client
    if not client.authenticated:
        return flash.redirect_with_flash(
            target, "error", "Er is geen Bitvavo-sleutel om te testen (BITVAVO_API_KEY)."
        )
    clock = time.monotonic()
    if _last_bitvavo_test and clock - _last_bitvavo_test < BITVAVO_TEST_REPEAT_S:
        return flash.redirect_with_flash(
            target, "warn", "Er is net een test gedaan. Wacht een paar seconden."
        )
    _last_bitvavo_test = clock
    try:
        account = await asyncio.wait_for(client.account(), BITVAVO_TEST_TIMEOUT_S)
    except BitvavoError as exc:
        if client.key_problem or client.trade_problem:
            state = bitvavo_key_state(request)
            return flash.redirect_with_flash(
                target, "error", f"Geweigerd door Bitvavo: {state['reason']} ({state['detail']})."
            )
        return flash.redirect_with_flash(
            target,
            "error",
            f"Niet getest: {errors.bitvavo_text(exc)} Probeer het over een paar minuten opnieuw.",
        )
    except Exception as exc:  # noqa: BLE001 - a time-out or worse: shown, never raised
        log.info("Bitvavo connection test failed: %s", exc)
        return flash.redirect_with_flash(
            target,
            "error",
            "Niet getest: Bitvavo antwoordde niet op tijd. Probeer het over een paar minuten "
            "opnieuw.",
        )
    fee = None
    try:
        fee = float((account.get("fees") or {})["taker"]) * 100
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    fee_text = f" Kosten per order (taker): {num(fee, 2)}%." if fee is not None else ""
    return flash.redirect_with_flash(
        target,
        "ok",
        f"De sleutel werkt: Bitvavo gaf je accountgegevens.{fee_text} Crypie kan niet zien of "
        "de sleutel mag handelen of geld mag opnemen; controleer bij Bitvavo dat hij alleen "
        "mag lezen en handelen.",
    )


@app.post("/settings/notify-test")
async def settings_notify_test(request: Request):
    """'Testmelding sturen' to the channel set in NOTIFY_URL."""
    ok, text = await send_test(request.app.state.env)
    return flash.redirect_with_flash(
        "/settings?saved=omgeving#omgeving", "ok" if ok else "error", text
    )


# -- Dashboard -------------------------------------------------------------------------


def _portfolio(balances: dict[str, float], prices_: dict[str, float]) -> dict | None:
    """Total value in EUR of cash plus coins at current prices, or None without data."""
    if not balances:
        return None
    cash = balances.get("EUR", 0.0)
    coins = sum(
        amount * prices_[f"{symbol}-EUR"]
        for symbol, amount in balances.items()
        if symbol != "EUR" and f"{symbol}-EUR" in prices_
    )
    return {
        "cash_eur": round(cash, 2),
        "coins_eur": round(coins, 2),
        "total_eur": round(cash + coins, 2),
    }


def _live_reminders(env: Settings, settings: Settings) -> list[str]:
    """Advice for live mode that is not enforced (enforcing it would stop live trading on the
    next deploy): own capital for the bot, and a restricted Bitvavo key."""
    out = []
    if settings.live_capital_eur <= 0:
        out.append(
            "LIVE_CAPITAL_EUR staat op 0: de limieten rekenen met je hele Bitvavo-account, ook "
            "met coins die je buiten de bot houdt. Stel het bedrag in dat de bot mag beheren, "
            "of gebruik een apart subaccount."
        )
    if not env.bitvavo_key_restricted:
        out.append(
            "Controleer de Bitvavo-sleutel: alleen de rechten 'bekijken' en 'handelen' (nooit "
            "'opnemen') en een IP-whitelist op het serveradres. Zet daarna "
            "BITVAVO_KEY_RESTRICTED=true in de serveromgeving; dan verdwijnt deze melding."
        )
    return out


async def _dashboard_context(
    request: Request,
    client: BitvavoClient,
    broker: Broker,
    budget: BudgetGuard,
    session: Session,
    error: str | None = None,
) -> dict:
    settings: Settings = request.app.state.settings
    prices_: dict[str, float] = {}
    balances: dict[str, float] = {}
    try:
        prices_ = await client.ticker_prices(settings.markets)
        balances = await broker.balances(session)
    except Exception as exc:
        error = error or str(exc)
    engine: TradingEngine = request.app.state.engine
    mode = engine.mode
    engine.remember_prices(prices_)
    strategy, strategy_warning = load_strategy_checked(session)
    paused = risk.is_paused(session)
    figures = risk_now(engine, session, settings, balances=balances or None, prices=prices_ or None)
    return {
        "settings": settings,
        # One status for the hero (app/trading/health.py) and the limits as current figures.
        "health": _health(
            request,
            engine,
            session,
            figures=figures,
            paused=paused,
            strategy_invalid=strategy_warning is not None,
        ),
        "risk_now": figures,
        "prices": prices_,
        "balances": balances,
        "portfolio": _portfolio(balances, prices_),
        "orders": recent_orders(session, 20),
        "error": error,
        "rate_limit": client.rate_limit_remaining,
        "budget": budget.status(session),
        "strategy": strategy,
        "strategy_warning": strategy_warning,
        "sentiments": [market_sentiment(session, c, 48) for c in settings.coins],
        "open_positions": open_positions(session, mode),
        "performance": performance_summary(session, mode),
        "paused": paused,
        "last_run": engine.last_run,
        "config_warning": request.app.state.config_warning,
        "unprotected_positions": engine.unprotected,
        "session_secret_weak": getattr(request.app.state, "session_secret_weak", False),
        "live_reminders": (
            _live_reminders(request.app.state.env, settings) if mode == "live" else []
        ),
        "chart_periods": list(PERIODS),
        "order_needs_password": mode == "live" and step_up_enabled(request),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    budget: BudgetGuard = Depends(get_budget),
    session: Session = Depends(get_session),
):
    ctx = await _dashboard_context(request, client, broker, budget, session)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.post("/orders")
async def dashboard_order(
    request: Request,
    market: str = Form(...),
    side: str = Form(...),
    amount_quote: float = Form(...),
    current_password: str = Form(""),
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    budget: BudgetGuard = Depends(get_budget),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    try:
        if engine.mode == "live":
            # Real money: ask for the current password again (step-up).
            await require_current_password(request, session, current_password)
        await engine.manual_order(session, market, side, amount_quote)
    except StepUpError as exc:
        ctx = await _dashboard_context(
            request, client, broker, budget, session, error=f"Order niet geplaatst: {exc.message}"
        )
        return templates.TemplateResponse(request, "dashboard.html", ctx, status_code=exc.status)
    except (ValueError, BitvavoError) as exc:
        ctx = await _dashboard_context(
            request, client, broker, budget, session, error=f"Order niet geplaatst: {exc}"
        )
        return templates.TemplateResponse(request, "dashboard.html", ctx, status_code=400)
    return RedirectResponse("/", status_code=303)
