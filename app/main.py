"""FastAPI entrypoint: JSON API plus a small dashboard."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import time
import weakref
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
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
from app.ai.shock_assessor import ShockAssessor
from app.ai.strategy_assistant import StrategyAssistant
from app.api_docs import redoc_page, swagger_page
from app.auth.oauth_routes import router as oauth_router
from app.auth.routes import (
    STEP_UP_MISSING,
    StepUpError,
    enrolment_unfinished,
    require_current_password,
    require_login_or_token,
    step_up_attempts_left,
    step_up_enabled,
)
from app.auth.routes import router as auth_router
from app.auth.service import (
    AuthService,
    load_or_create_session_secret,
    session_secret_is_weak,
)
from app.bitvavo.broker import (
    PAPER_WALLET_KEY,
    Broker,
    LiveBroker,
    OrderUncertain,
    PaperBroker,
    recent_orders,
)
from app.bitvavo.client import BitvavoClient, BitvavoError, InvalidMarketError
from app.config import Settings, get_settings
from app.db import Evaluation, Order, Position, SessionLocal, Setting, get_session, init_db
from app.labels import limit_pct
from app.mcp_server import router as mcp_router
from app.news.registry import backfill_events
from app.news.service import (
    FEAR_GREED_NL,
    NEWS_RETENTION_DAYS,
    SENTIMENT_WINDOW_H,
    NewsService,
    annotate_groups,
    clamp_future_items,
    clear_market_mood,
    count_items,
    count_status,
    event_key_for,
    fact_anchor,
    group_items,
    load_market_mood,
    market_sentiment,
    recent_items,
    requeue_items_without_event_key,
    text_lang,
)
from app.news.sources import NewsFetcher
from app.notify import Notifier, notify_configured, send_test
from app.numbers import AmbiguousNumber, ambiguous_nl, split_decimal, strip_spaces
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
from app.strategy.page import (
    RESULT_TOKEN,
    RESULTS,
    StrategyInputError,
    backtest_view,
    curve_svg,
    equity_curves,
    parse_strategy_json,
    save_confirm,
)
from app.strategy.store import (
    STRATEGY_KEY,
    load_previous,
    load_strategy,
    load_strategy_checked,
    save_strategy,
    strategy_version,
)
from app.trading import display, risk
from app.trading.engine import (
    EXCHANGE_MIN_ORDER_EUR,
    NEWS_ENTRY_MIN_SCORE,
    NEWS_ENTRY_SIZE_FACTOR,
    NEWS_EXIT_MAX_SCORE,
    NEWS_MIN_SOURCES,
    EvaluationBusy,
    TradingEngine,
    closed_positions,
    latest_decisions,
    open_position,
    open_positions,
    performance_summary,
    recent_decisions,
)
from app.trading.events import parse_calendar
from app.trading.health import bot_health, describe_problem, risk_now
from app.trading.history import (
    PERIODS,
    blocked_buy_review,
    candle_dicts,
    market_actions,
    period_spec,
    period_start,
    portfolio_history,
    trade_actions,
)
from app.trading.journal import (
    BY_NL as JOURNAL_SOURCES_NL,
)
from app.trading.journal import (
    EXIT_NL,
    JournalFilter,
    all_tags,
    journal_entries,
    journal_entry,
    journal_totals,
    mode_counts,
    save_note,
    unlinked_orders,
)
from app.trading.journal import (
    journal_page as journal_entries_page,
)
from app.trading.plan import PLAN_FIELDS, PLAN_MAX_LENGTH, TradingPlan, load_plan, save_plan
from app.trading.risk import RiskLimits
from app.trading.trend import load_trends, refresh_trends
from app.web import STATIC_DIR, refusal_nl, templates
from app.web import amount as fmt_amount
from app.web import eur as fmt_eur
from app.web import nl as fmt_nl
from app.web import num as fmt_num
from app.web import price_eur as fmt_price_eur

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
        elif not auth.user(session).mfa_enrolled:
            # Never the setup token itself once the account exists: whoever reads the logs
            # must not be able to link an authenticator with it (see enrolment_unfinished).
            if auth.setup_token_from_env and enrolment_unfinished(auth.user(session)):
                log.warning(
                    "the account has no authenticator linked yet: open /setup?token="
                    "<SETUP_TOKEN> in the browser that made the account and log in with the "
                    "password to link one, or run: python -m app.auth reset-mfa"
                )
            else:
                log.warning(
                    "the account has no authenticator linked yet: run "
                    "python -m app.auth reset-mfa and enter the code it prints at /login"
                )
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
    if engine.shock_assessor is not None:
        engine.shock_assessor.model = new.claude_model_shock
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
    engine.shock_mode = settings.shock_mode
    engine.shock_min_hours = settings.shock_min_hours
    engine.shock_max_hours = settings.shock_max_hours
    engine.shock_recheck_min = settings.shock_recheck_min


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
        try:
            # The event registry behind the shock assessment: one row per news fact, filled
            # from the stored news the first time (and for any key that has none yet).
            created = backfill_events(session)
            if created:
                log.info("news: %d event(s) added to the event registry", created)
        except Exception:  # noqa: BLE001 - never block startup on housekeeping
            session.rollback()
            log.exception("backfilling the event registry failed")
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
        shock_assessor=ShockAssessor(
            settings.anthropic_api_key, settings.claude_model_shock, app.state.budget
        ),
    )
    engine: TradingEngine = app.state.engine
    engine.trend_context = settings.trend_context_enabled
    engine.market_mood_enabled = settings.fear_greed_enabled
    engine.live_capital_eur = settings.live_capital_eur
    engine.broker_for_mode = lambda mode: exit_broker(app, mode)
    _apply_risk_off(engine, settings)
    # News shocks: the registry records each fact's price at first sight, and a refresh that
    # scored articles has the shock guard judge new triggers right away.
    app.state.news.price_lookup = lambda: dict(engine.last_prices)
    app.state.news.after_assess = engine.shock.on_news
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
        asyncio.create_task(shock_scheduler(app)),
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


async def shock_scheduler(app: FastAPI) -> None:
    """Every minute while risk-off is on: judge new news-shock triggers (call A), recheck the
    shock pauses whose recheck is due (call B) and close the ones that ran out. Independent of
    TRADING_ENABLED: a pause must end on time even while the bot does not trade."""

    async def job(session: Session) -> None:
        engine: TradingEngine = app.state.engine
        await engine.shock.tick(session)

    await _run_periodically(
        app,
        "shock check",
        job,
        lambda s: 60 if s.risk_off_enabled else 0,
        initial_delay=25,
        persist=False,
        timeout=600,
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
    responses=errors.API_ERROR_RESPONSES,
)
if _docs:
    app.get("/docs", include_in_schema=False)(lambda: swagger_page(app.title, app.openapi_url))
    app.get("/redoc", include_in_schema=False)(lambda: redoc_page(app.title, app.openapi_url))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# The login check, with the API token (API_TOKEN) as a second door for /api/* and /mcp.
app.middleware("http")(require_login_or_token)
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
app.include_router(auth_router, include_in_schema=False)
app.include_router(mcp_router)
app.include_router(oauth_router)


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


# -- API helpers: errors and pages ------------------------------------------------------

CLAUDE_MISSING = (
    "Claude is niet ingesteld: de Claude-sleutel ontbreekt (ANTHROPIC_API_KEY in de "
    "serveromgeving)."
)


def _claude_missing(text: str = CLAUDE_MISSING) -> errors.ApiError:
    return errors.ApiError(503, text, "claude_unavailable")


def _budget_error(exc: Exception, prefix: str = "") -> errors.ApiError:
    return errors.ApiError(429, f"{prefix}{exc}", "claude_budget_exceeded")


def _step_up_error(exc: StepUpError) -> errors.ApiError:
    """The step-up (current password) refused: 401, 403 (missing or wrong) or 429 (wait)."""
    if exc.status == 403:
        code = "password_required" if exc.message == STEP_UP_MISSING else "wrong_password"
    else:
        code = errors.API_CODES.get(exc.status, "error")
    return errors.ApiError(exc.status, exc.message, code)


# Lists are paged with an opaque cursor: a page that has more after it carries a
# ``Link: <...&cursor=...>; rel="next"`` header (RFC 8288), so the body keeps its shape.
CURSOR_MAX = 200


def _bad_cursor() -> errors.ApiError:
    return errors.ApiError(
        400,
        "Ongeldige cursor: neem hem over uit de Link-header van de vorige pagina.",
        "invalid_cursor",
    )


def encode_cursor(**values: Any) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, *keys: str) -> dict | None:
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except ValueError:  # bad base64 (binascii.Error), bad UTF-8 or bad JSON
        values = None
    if not isinstance(values, dict) or set(values) != set(keys):
        raise _bad_cursor()
    return values


def _cursor_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DB_ID_MAX:
        raise _bad_cursor()
    return value


def id_cursor(cursor: str | None) -> int | None:
    """The id a page continues below (lists ordered by id, newest first)."""
    values = _decode_cursor(cursor, "id")
    return None if values is None else _cursor_id(values["id"])


def time_id_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    """(moment, id) a page continues below (lists ordered by a moment, then id)."""
    values = _decode_cursor(cursor, "at", "id")
    if values is None:
        return None
    try:
        at = datetime.fromisoformat(str(values["at"]))
    except ValueError:
        raise _bad_cursor() from None
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    return at, _cursor_id(values["id"])


def set_next_link(request: Request, response: Response, cursor: str) -> None:
    url = request.url.include_query_params(cursor=cursor)
    response.headers["Link"] = f'<{url.path}?{url.query}>; rel="next"'


def page_of(request: Request, response: Response, rows: list, limit: int, cursor_of) -> list:
    """The first ``limit`` of ``rows`` (fetched with limit + 1); when there were more, a Link
    header to the next page, whose cursor ``cursor_of(last row)`` makes."""
    if len(rows) > limit:
        rows = rows[:limit]
        set_next_link(request, response, cursor_of(rows[-1]))
    return rows


CURSOR_QUERY = Query(
    None,
    max_length=CURSOR_MAX,
    description="Volgende pagina: neem de cursor over uit de Link-header (rel=next)",
)


# -- API -------------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/prices")
async def prices(request: Request, client: BitvavoClient = Depends(get_client)):
    try:
        return await client.ticker_prices(request.app.state.settings.markets)
    except BitvavoError as exc:  # network trouble arrives as a BitvavoError too
        raise errors.bitvavo_api_error(exc) from exc


# Path and query bounds. A market goes into the Bitvavo URL path, so only the strict
# Bitvavo notation is accepted (no '..', '?' or '#'); ids fit the database's integer columns.
MARKET_PATH = Path(pattern=r"^[A-Za-z0-9]{2,10}-[Ee][Uu][Rr]$")
DB_ID_MAX = 2**31 - 1
INTERVAL_PATTERN = "^(" + "|".join((*INTERVALS, "1W", "1M")) + ")$"  # what Bitvavo offers


def _tracked_market(request: Request, market: str) -> str:
    market = market.upper()
    if market not in request.app.state.settings.markets:
        raise errors.ApiError(404, f"Markt {market} wordt niet gevolgd.", "market_not_tracked")
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
        raise errors.ApiError(422, str(exc), "invalid_market") from exc
    except BitvavoError as exc:
        raise errors.bitvavo_api_error(exc) from exc


# The overview charts (UX-102): the same short wait as the overview itself, and once Bitvavo
# did not answer, no chart asks again for PRICE_REUSE_S seconds (each would wait in vain).
CHART_TIMEOUT_S = 5.0
# Per Bitvavo client: when a chart or the overview last found Bitvavo unreachable, and why.
_bitvavo_outages: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ChartUnavailable(Exception):
    """A chart cannot be drawn: ``reason`` is 'unreachable' (time-out, no connection, a
    Bitvavo outage: try again later), 'refused' (Bitvavo refused the request, e.g. a market it
    does not know: trying again will not help), 'key' or 'busy'; ``message`` is Dutch."""

    def __init__(self, reason: str, message: str, market: str | None = None):
        super().__init__(message)
        self.reason, self.message, self.market = reason, message, market


def _unreachable(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, BitvavoError) and (exc.status == 0 or exc.status >= 500)
    )


def _note_outage(client: object, exc: BaseException, message: str | None = None) -> None:
    """Remember that Bitvavo did not answer (``message``: how the caller put it)."""
    if _unreachable(exc):
        _bitvavo_outages[client] = (time.monotonic(), message or _chart_problem(exc).message)


def _recent_outage(client: object) -> str | None:
    hit = _bitvavo_outages.get(client)
    if hit and time.monotonic() - hit[0] <= PRICE_REUSE_S:
        return hit[1]
    return None


def _chart_problem(exc: BaseException, market: str | None = None) -> ChartUnavailable:
    """Why Bitvavo gave no candles, as a ChartUnavailable (UX-53: a refusal is not an outage)."""
    if isinstance(exc, TimeoutError):
        text = f"Bitvavo antwoordde niet binnen {CHART_TIMEOUT_S:g} seconden"
        return ChartUnavailable("unreachable", text, market)
    if not isinstance(exc, BitvavoError):
        return ChartUnavailable("unreachable", describe_problem(exc), market)
    if exc.status == 0:
        return ChartUnavailable(
            "unreachable", "Bitvavo reageert niet (geen verbinding of een time-out)", market
        )
    if exc.status >= 500:
        return ChartUnavailable(
            "unreachable", f"Bitvavo had een storing (fout {exc.status})", market
        )
    if exc.rate_limited:
        return ChartUnavailable(
            "busy", "Bitvavo krijgt even te veel verzoeken; probeer het over een minuut", market
        )
    if exc.status in (401, 403) or exc.code in KEY_PROBLEMS_NL:
        return ChartUnavailable("key", _bitvavo_problem(exc), market)
    where = f" voor {market}" if market else ""
    detail = f"fout {exc.status}" + (f", code {exc.code}" if exc.code is not None else "")
    said = exc.payload.get("error") if isinstance(exc.payload, dict) else exc.payload
    said = str(said or "").strip().rstrip(".")[:160]
    return ChartUnavailable(
        "refused",
        f"Bitvavo weigerde het verzoek{where} ({detail}{': ' + said if said else ''})",
        market,
    )


async def _chart_candles(client: BitvavoClient, market: str, interval: str, limit: int):
    """Candles for a chart, waiting at most CHART_TIMEOUT_S; raises ChartUnavailable."""
    outage = _recent_outage(client)
    if outage is not None:
        raise ChartUnavailable("unreachable", outage + " (net nog geprobeerd)", market)
    try:
        rows = await asyncio.wait_for(client.candles(market, interval, limit), CHART_TIMEOUT_S)
    except (BitvavoError, TimeoutError) as exc:
        _note_outage(client, exc)
        raise _chart_problem(exc, market) from exc
    _bitvavo_outages.pop(client, None)
    return rows


def _chart_error(problem: ChartUnavailable) -> JSONResponse:
    """502 with the reason for chart.js: 'unreachable' and 'busy' offer to try again."""
    return JSONResponse(
        {
            "detail": problem.message,
            "code": f"bitvavo_{problem.reason}",
            "reason": problem.reason,
            "market": problem.market,
        },
        status_code=502,
    )


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
        rows = await _chart_candles(client, market, interval, limit)
    except ChartUnavailable as problem:
        return _chart_error(problem)
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
        "actions": [
            {**a, "detail": refusal_nl(a["detail"])} if a["kind"] == "blocked" else a
            for a in market_actions(session, mode, market, since, interval)
        ],
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
    results = await asyncio.gather(
        *(_chart_candles(client, m, interval, limit) for m in settings.markets),
        return_exceptions=True,
    )
    # One market Bitvavo refuses (a typo, a delisted coin) must not take the whole chart with
    # it (UX-53): leave it out and say so. Only when every market fails is it an error.
    failed = [
        (m, r)
        for m, r in zip(settings.markets, results, strict=True)
        if isinstance(r, BaseException)
    ]
    for _, exc in failed:
        if not isinstance(exc, ChartUnavailable):
            raise exc
    unreachable = [exc for _, exc in failed if exc.reason == "unreachable"]
    if unreachable or (settings.markets and len(failed) == len(settings.markets)):
        # Bitvavo is down (or nothing at all came back): no half-rebuilt portfolio.
        return _chart_error(unreachable[0] if unreachable else failed[0][1])
    try:
        if broker.mode == "paper":
            balances = await broker.balances(session)
        else:
            balances = await asyncio.wait_for(broker.balances(session), CHART_TIMEOUT_S)
    except (BitvavoError, TimeoutError) as exc:
        _note_outage(client, exc)
        return _chart_error(_chart_problem(exc))
    candles_by_market = {
        m: candle_dicts(rows)
        for m, rows in zip(settings.markets, results, strict=True)
        if not isinstance(rows, BaseException)
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
        "skipped": [{"market": m, "reason": exc.message} for m, exc in failed],
    }


@app.get("/api/balance")
async def balance(broker: Broker = Depends(get_broker), session: Session = Depends(get_session)):
    try:
        return {"mode": broker.mode, "balances": await broker.balances(session)}
    except BitvavoError as exc:
        raise errors.bitvavo_api_error(exc) from exc


# Step-up over the API: the current password in a header, for calls without a JSON body.
PASSWORD_HEADER = "x-current-password"


# An Idempotency-Key makes a retried order safe (see create_order).
IDEMPOTENCY_KEY_MAX = 128
ORDER_UNCERTAIN_API = (
    "Order verstuurd, maar de uitkomst is nog niet bekend: Bitvavo bevestigde hem niet op tijd. "
    "De bot controleert het vanzelf; volg de order via de Location-header en plaats hem niet "
    "opnieuw."
)
ORDER_RESENT_API = (
    "Order niet geplaatst: deze Idempotency-Key is al gebruikt, dus de order is niet nog een "
    "keer geplaatst. Kijk bij de orders wat er gebeurde."
)


class OrderIn(BaseModel):
    market: str = Field(max_length=32)
    side: Literal["buy", "sell"]
    # Range and finiteness are checked by TradingEngine.manual_order (422 with a reason); a
    # pydantic error would echo a NaN input back and fail to serialise.
    amount_quote: float = Field(description="Amount in EUR")
    reason: str | None = Field(default=None, max_length=200)
    # Needed while the bot trades live (step-up, like the /account page).
    current_password: str | None = Field(default=None, max_length=1024)
    # The Idempotency-Key header as a field, for clients that cannot set headers (MCP).
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=IDEMPOTENCY_KEY_MAX)


class OrderOut(BaseModel):
    """An order as the API shows it (documentation; the handlers return plain dicts)."""

    id: int
    created_at: datetime
    mode: Literal["paper", "live"]
    market: str
    side: Literal["buy", "sell"]
    amount: float | None = Field(description="In de munt zelf")
    amount_quote: float | None = Field(description="In euro's")
    fee_quote: float | None = Field(description="Handelskosten in euro's")
    price: float | None
    status: str = Field(
        description="pending (nog niet verstuurd), rejected, unknown (verstuurd, uitkomst nog "
        "onbekend) of een Bitvavo-status zoals filled"
    )
    reason: str | None


class OrderAcceptedOut(OrderOut):
    message: str


def _order_dict(o: Order) -> dict:
    return {
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


@app.get("/api/orders", responses={200: {"model": list[OrderOut]}})
async def list_orders(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=500),
    mode: Literal["paper", "live"] | None = Query(
        None, description="Alleen paper of live; leeg = allebei"
    ),
    cursor: str | None = CURSOR_QUERY,
    session: Session = Depends(get_session),
):
    """The newest orders first, paper and live (or one ``mode``); more via the Link header."""
    rows = recent_orders(session, limit + 1, mode=mode, before_id=id_cursor(cursor))
    rows = page_of(request, response, rows, limit, lambda o: encode_cursor(id=o.id))
    return [_order_dict(o) for o in rows]


@app.get("/api/orders/{order_id}", responses={200: {"model": OrderOut}})
async def get_order(
    order_id: int = Path(ge=1, le=DB_ID_MAX), session: Session = Depends(get_session)
):
    """One order, e.g. to follow one that answered 202 (outcome not known yet)."""
    order = session.get(Order, order_id)
    if order is None:
        raise errors.ApiError(404, f"Order {order_id} bestaat niet.", "order_not_found")
    return _order_dict(order)


@app.post(
    "/api/orders",
    status_code=201,
    responses={
        201: {"model": OrderOut, "description": "Geplaatst; Location wijst naar de order"},
        202: {
            "model": OrderAcceptedOut,
            "description": "Verstuurd, uitkomst nog onbekend: volg Location, niet opnieuw sturen",
        },
    },
)
async def create_order(
    order: OrderIn,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX,
        description="Een unieke tekst per order: dezelfde sleutel plaatst binnen 30 minuten "
        "niets opnieuw (409 order_resent)",
    ),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """A manual order under the bot's rules: see ``TradingEngine.manual_order``.

    While the bot trades live the current password is needed as well. With an
    ``Idempotency-Key`` (header, or the ``idempotency_key`` field) a retry is safe: the same key
    places nothing again for 30 minutes (409 ``order_resent``), like the one-time token of the
    order form. An order that went out but whose outcome Bitvavo did not confirm in time
    answers 202 with the order (status pending or unknown) and a Location to follow it: it may
    still be filled, so do not send it again.
    """
    if engine.mode == "live":
        password = order.current_password or request.headers.get(PASSWORD_HEADER)
        try:
            await require_current_password(request, session, password)
        except StepUpError as exc:
            raise _step_up_error(exc) from exc
    key = order.idempotency_key or idempotency_key
    token = f"api:{key}" if key else ""
    if token and not _claim_order_token(token):
        raise errors.ApiError(409, ORDER_RESENT_API, "order_resent")
    try:
        o = await engine.manual_order(
            session, order.market, order.side, order.amount_quote, order.reason
        )
    except OrderUncertain as exc:
        body = {**_order_dict(exc.order), "message": ORDER_UNCERTAIN_API}
        return JSONResponse(
            jsonable_encoder(body),
            status_code=202,
            headers={"Location": f"/api/orders/{exc.order.id}"},
        )
    except ValueError as exc:
        # Refused by Crypie's own checks (or an invalid market name) before anything was sent:
        # no order exists, so the same key may be used again after a fix.
        _release_order_token(token, bool(token))
        raise errors.ApiError(422, f"Order niet geplaatst: {exc}.", "order_refused") from exc
    except BitvavoError as exc:
        raise errors.bitvavo_api_error(exc) from exc
    response.headers["Location"] = f"/api/orders/{o.id}"
    return _order_dict(o)


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
    strategy_curve, hold_curve = equity_curves(candles, result, strategy.position_size_pct)
    return {
        **result.summary(),
        "market": strategy.market,
        "interval": strategy.interval,
        "first_time": int(candles[0][0]) if candles else None,
        "last_time": int(candles[-1][0]) if candles else None,
        "wins": result.wins,  # net, like the journal (UX-106)
        # The value of the test account per candle (sampled), for the chart on the page.
        "curve": curve_svg(strategy_curve, hold_curve),
        "latest": latest_snapshot(strategy, candles),
        "trade_log": [
            {
                "entry_time": t.entry_time,
                "entry_price": t.entry_price,
                "exit_time": t.exit_time,
                "exit_price": t.exit_price,
                "pnl_pct": round(t.pnl_pct, 2) if t.pnl_pct is not None else None,
                "net_pnl_pct": round(net, 2)
                if (net := t.net_pnl_pct(result.fee_pct)) is not None
                else None,
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
        raise errors.ApiError(
            422,
            f"{strategy.market} staat niet bij de markten van de bot (MARKETS).",
            "market_not_tracked",
            fields={"market": "staat niet in MARKETS"},
        )
    try:
        check_for_use(strategy)
    except ValueError as exc:
        raise errors.ApiError(422, str(exc), "invalid_strategy") from exc
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
        raise _claude_missing()
    try:
        proposal = await assistant.propose(
            session, body.description, load_strategy(session), request.app.state.settings.markets
        )
    except BudgetExceeded as exc:
        raise _budget_error(exc) from exc
    except ValueError as exc:  # Claude chose a market the bot does not follow
        raise errors.ApiError(422, f"{exc}.", "market_not_tracked") from exc
    except RuntimeError as exc:
        raise errors.ApiError(502, str(exc), "claude_error") from exc
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
        raise errors.bitvavo_api_error(exc) from exc
    except (ValueError, ArithmeticError) as exc:
        raise errors.ApiError(422, f"Backtest mislukt: {exc}", "backtest_failed") from exc


def _waiting_proposal(session: Session, settings: Settings, current: Strategy) -> dict | None:
    """The proposal that waits at the top of Handelen (``#voorstel``), so the strategy page can
    point to it (UX-15). Same rule as Handelen: the newest unapplied proposal of the last
    evaluations, and only when it can be applied as it is; an older one is not offered."""
    for ev in session.scalars(select(Evaluation).order_by(Evaluation.id.desc()).limit(5)):
        if not ev.proposed_strategy or ev.applied:
            continue
        proposal = _proposal(ev)
        if proposal is None or _proposal_problems(ev, current, settings):
            return None
        return {"name": proposal.name, "created_at": ev.created_at, "id": ev.id}
    return None


def _strategy_context(
    request: Request,
    session: Session,
    assistant: StrategyAssistant,
    state: dict | None = None,
) -> dict:
    """The strategy page. ``state`` is the outcome of a form (a test, a proposal, refused
    input or a conflict), from the result store or straight from the POST."""
    settings: Settings = request.app.state.settings
    current, load_warning = load_strategy_checked(session)
    state = state or {}
    live = settings.trading_mode == "live"
    previous = load_previous(session)
    ctx: dict = {
        "settings": settings,
        "mode": settings.trading_mode,
        "strategy": current,
        "strategy_json": state.get("strategy_json") or current.model_dump_json(indent=2),
        "version": strategy_version(session),
        "load_warning": load_warning,
        "assistant_available": assistant.available,
        "assistant_model": settings.claude_model_strategy,
        "waiting": _waiting_proposal(session, settings, current),
        "previous": previous[0] if previous else None,
        "previous_at": previous[1] if previous else None,
        "previous_confirm": None,
        "result_missing": state.get("missing", False),
        "kind": state.get("kind"),
        "description": state.get("description", ""),
        "limit": state.get("limit", 500),
        "test_market": state.get("market", ""),
        "errors": state.get("errors", []),
        "error_title": state.get("error_title"),
        "json_context": state.get("json_context", []),
        "claude_error": state.get("claude_error"),
        "message": state.get("message"),
        "proposal": None,
        "proposal_json": None,
        "proposal_diff": None,
        "problems": [],
        "explanation": state.get("explanation"),
        "backtest": state.get("backtest"),
        "bt": backtest_view(state["backtest"]) if state.get("backtest") else None,
        "save_confirm": None,
        "conflict": None,
        "backtest_error": state.get("backtest_error"),
        "editor_confirm": ("LIVE · echt geld: " if live else "")
        + "Opslaan zonder test? De strategie in de editor wordt meteen actief op "
        + ", ".join(settings.markets)
        + ", zonder backtest.",
        "reset_confirm": (
            ("LIVE · echt geld: " if live else "")
            + f"De actieve strategie wordt vervangen door de standaardstrategie "
            f"'{DEFAULT_STRATEGY.name}' van Crypie, op {', '.join(settings.markets)}. De huidige "
            "blijft bewaard als vorige strategie. Doorgaan?"
        ),
    }
    if ctx["previous"] is not None:
        ctx["previous_confirm"] = (
            ("LIVE · echt geld: " if live else "")
            + f"Vorige strategie '{ctx['previous'].name}' terugzetten en meteen actief maken "
            f"op {', '.join(settings.markets)}? De huidige wordt dan de vorige."
        )
    if state.get("proposal"):
        proposal = Strategy.model_validate(state["proposal"])
        ctx["proposal"] = proposal
        ctx["proposal_json"] = proposal.model_dump_json(indent=2)
        ctx["proposal_diff"] = strategy_diff(current, proposal)
        problems = use_problems(proposal)
        if proposal.market not in settings.markets:
            problems.append(f"{proposal.market} staat niet in je markten (MARKETS)")
        ctx["problems"] = problems
        ctx["save_confirm"] = save_confirm(
            proposal.name,
            settings.markets,
            state.get("backtest"),
            live,
            tested=bool(state.get("backtest")),
        )
    if state.get("conflict"):
        yours = Strategy.model_validate(state["conflict"])
        ctx["conflict"] = {
            "strategy": yours,
            "json": yours.model_dump_json(indent=2),
            "diff": strategy_diff(current, yours),
            "confirm": ("LIVE · echt geld: " if live else "")
            + f"Toch overschrijven? De strategie '{current.name}' die nu actief is, wordt "
            f"vervangen door jouw versie '{yours.name}'.",
        }
    return ctx


async def _try_backtest(
    request: Request, client: BitvavoClient, strategy: Strategy, limit: int, state: dict
) -> None:
    """Run the backtest into ``state``; a failure is said on the page, not lost with the
    strategy (a proposal from Claude cost money)."""
    try:
        state["backtest"] = await _run_backtest(request, client, strategy, limit, 1000)
    except BitvavoError as exc:
        state["backtest_error"] = "De backtest kon niet draaien: " + errors.bitvavo_text(exc)
    except (ValueError, ArithmeticError) as exc:
        state["backtest_error"] = f"De backtest kon niet draaien: {exc}"


def _strategy_input(strategy_json: str, settings: Settings) -> Strategy:
    """The editor's strategy, or StrategyInputError with Dutch errors next to the editor."""
    if len(strategy_json) > STRATEGY_JSON_MAX:
        limit = f"{STRATEGY_JSON_MAX:,}".replace(",", ".")
        raise StrategyInputError(
            [
                {
                    "where": "Strategie",
                    "path": "",
                    "message": f"de JSON is te lang (maximaal {limit} tekens)",
                }
            ]
        )
    candidate = parse_strategy_json(strategy_json)
    if candidate.market not in settings.markets:
        raise StrategyInputError(
            [
                {
                    "where": "Backtest-markt",
                    "path": "market",
                    "message": f"{candidate.market} staat niet in je markten (MARKETS: "
                    f"{', '.join(settings.markets)}). Dit veld bepaalt alleen op welke markt de "
                    "backtest draait; de strategie geldt voor al je markten",
                }
            ]
        )
    return candidate


@app.get("/strategy", response_class=HTMLResponse, include_in_schema=False)
async def strategy_page(
    request: Request,
    result: str | None = Query(None, max_length=64),
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    state = None
    if result:
        state = RESULTS.get(result) if RESULT_TOKEN.match(result) else None
        state = state or {"missing": True}
    ctx = _strategy_context(request, session, assistant, state)
    return templates.TemplateResponse(request, "strategy.html", ctx)


# Where each outcome lands on the page.
STRATEGY_ANCHORS = {"invalid": "editor", "claude": "claude", "conflict": "conflict"}


@app.post("/strategy", response_class=HTMLResponse, include_in_schema=False)
async def strategy_action(
    request: Request,
    action: str = Form(..., max_length=32),
    strategy_json: str = Form(""),
    description: str = Form(""),
    limit: int = Form(500, ge=50, le=MAX_CANDLES),
    market: str = Form("", max_length=16),
    base_version: str = Form("", max_length=64),
    client: BitvavoClient = Depends(get_client),
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    """Test, propose, save, reset or put back the previous strategy.

    Browsers always get a redirect (Post/Redirect/Get): a success to the page with a message, a
    test or proposal to ``/strategy?result=<id>``, so a reload never runs a test or a paid
    Claude call again. Other clients get the page itself, with 400 for refused input.
    """
    settings: Settings = request.app.state.settings
    html = errors.wants_html(request)
    state: dict = {"limit": limit, "market": market if market in settings.markets else ""}
    done: str | None = None  # the message after a change that went through
    status = 200
    try:
        if action == "propose":
            state.update(kind="claude", description=description[:DESCRIPTION_MAX])
            if not assistant.available:
                raise RuntimeError("Voorstellen via Claude staan uit: er is geen Claude-sleutel.")
            # Same bounds as POST /api/strategy/propose.
            try:
                ProposalIn(description=description)
            except ValidationError as exc:
                raise RuntimeError(
                    f"schrijf je wens in minstens 5 en hoogstens {DESCRIPTION_MAX} tekens."
                ) from exc
            current = load_strategy(session)
            proposal = await assistant.propose(session, description, current, settings.markets)
            state.update(
                kind="propose",
                proposal=proposal.strategy.model_dump(mode="json"),
                strategy_json=proposal.strategy.model_dump_json(indent=2),
                explanation=proposal.explanation,
            )
            await _try_backtest(request, client, proposal.strategy, limit, state)
        elif action == "reset":
            save_strategy(session, DEFAULT_STRATEGY)
            done = (
                f"Standaardstrategie '{DEFAULT_STRATEGY.name}' hersteld en actief op "
                f"{', '.join(settings.markets)}. De vorige strategie is bewaard."
            )
        elif action == "restore_previous":
            previous = load_previous(session)
            if previous is None:
                raise ValueError("Er is geen vorige strategie bewaard.")
            if base_version and base_version != strategy_version(session):
                raise ValueError(
                    "De strategie is intussen gewijzigd; terugzetten zou die wijziging ongedaan "
                    "maken. Bekijk de pagina opnieuw en kies dan."
                )
            strategy = previous[0]
            if strategy.market not in settings.markets:
                raise ValueError(f"{strategy.market} staat niet meer in je markten (MARKETS).")
            check_for_use(strategy)
            save_strategy(session, strategy)
            done = (
                f"Vorige strategie '{strategy.name}' teruggezet en actief op "
                f"{', '.join(settings.markets)}. De strategie die er stond, is nu de vorige."
            )
        elif action in ("backtest", "save"):
            state.update(kind="invalid", strategy_json=strategy_json[:STRATEGY_JSON_MAX])
            candidate = _strategy_input(strategy_json, settings)
            if action == "backtest":
                state.update(
                    kind="backtest",
                    proposal=candidate.model_dump(mode="json"),
                    strategy_json=candidate.model_dump_json(indent=2),
                )
                # The market chosen for the test is only where the test runs: it does not
                # change the strategy, so testing the active one elsewhere is no new strategy.
                tested = candidate
                if state["market"] and state["market"] != candidate.market:
                    tested = candidate.model_copy(update={"market": state["market"]})
                await _try_backtest(request, client, tested, limit, state)
            else:
                try:
                    check_for_use(candidate)
                except ValueError as exc:
                    raise StrategyInputError(
                        [{"where": "Strategie", "path": "", "message": str(exc)}]
                    ) from exc
                if base_version and base_version != strategy_version(session):
                    # Changed since this page was loaded (another tab, an applied proposal):
                    # show what saving would undo instead of overwriting it silently.
                    state.update(kind="conflict", conflict=candidate.model_dump(mode="json"))
                    status = 409
                else:
                    save_strategy(session, candidate)
                    done = (
                        f"Strategie '{candidate.name}' opgeslagen en actief op "
                        f"{', '.join(settings.markets)}. De vorige strategie is bewaard."
                    )
        else:
            raise ValueError(f"Onbekende actie {action!r}")
    except StrategyInputError as exc:
        state.update(
            kind="invalid",
            error_title="Ongeldige strategie, niet opgeslagen:"
            if action == "save"
            else "Ongeldige strategie, niet getest:",
            errors=exc.errors,
            json_context=exc.context,
        )
        status = 400
    except (BudgetExceeded, RuntimeError, ValueError) as exc:
        # Claude could not make a proposal (no key, budget, an unusable answer), or a reset,
        # a restore or an unknown action was refused.
        status = 400
        if action == "propose":
            state.update(kind="claude", claude_error=f"Geen voorstel gemaakt: {exc}")
        elif html:
            return flash.redirect_with_flash("/strategy#actief", "error", str(exc))
        else:
            state = {"message_error": str(exc)}
    if done is not None:
        if html:
            return flash.redirect_with_flash("/strategy#actief", "ok", done)
        state = {"message": done}
    elif html:
        token = RESULTS.put(state)
        anchor = STRATEGY_ANCHORS.get(state.get("kind") or "", "resultaat")
        if state.get("kind") in ("backtest", "propose") and status == 400:
            anchor = "editor"
        return RedirectResponse(f"/strategy?result={token}#{anchor}", status_code=303)
    ctx = _strategy_context(request, session, assistant, state)
    if state.get("message_error"):
        ctx["error_title"] = state["message_error"]
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
    response: Response,
    hours: int = Query(24, ge=1, le=NEWS_HOURS_MAX),
    limit: int = Query(100, ge=1, le=500),
    counts: bool | None = None,
    cursor: str | None = CURSOR_QUERY,
    session: Session = Depends(get_session),
):
    """The newest articles first; more via the Link header. ``counts`` filters the page after
    it is read, so a filtered page can hold fewer than ``limit`` articles."""
    tracked = request.app.state.settings.coins
    items = recent_items(session, hours, limit + 1, before=time_id_cursor(cursor))
    items = page_of(
        request,
        response,
        items,
        limit,
        lambda it: encode_cursor(at=it.published_at.isoformat(), id=it.id),
    )
    rows = [_news_dict(it, tracked) for it in items]
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


# The news page groups every article of the chosen period (no silent cap): 30 news facts per
# page, counted over the whole period. Only a period with more articles than this is cut, and
# the page then says so.
NEWS_PAGE_SIZE = 30
NEWS_PAGE_MAX_ITEMS = 5000
NEWS_PERIODS = ((6, "6 uur"), (24, "24 uur"), (48, "48 uur"), (72, "3 dagen"))
NEWS_DEFAULT_HOURS = SENTIMENT_WINDOW_H  # the window the sentiment and 'telt mee' use


def _news_url(hours: int, telt: str = "", page: int = 1, anchor: str = "") -> str:
    params = {"hours": hours}
    if telt:
        params["telt"] = telt
    if page > 1:
        params["page"] = page
    return f"/news?{urlencode(params)}" + (f"#{anchor}" if anchor else "")


def _news_groups(session: Session, settings: Settings, hours: int) -> tuple[list, int, bool]:
    """Every news fact of the last ``hours`` with its status, the number of articles in that
    window, and whether the window had more articles than the page groups."""
    items = recent_items(session, hours, NEWS_PAGE_MAX_ITEMS)
    total = count_items(session, hours)
    groups = annotate_groups(group_items(items), settings.coins)
    return groups, total, total > len(items)


def _news_refresh_flash(result: dict) -> tuple[str, str]:
    """One message after 'Nu verversen': what came in and what Claude did with it."""
    n = int(result.get("inserted") or 0)
    new = (
        "geen nieuwe berichten"
        if n == 0
        else ("1 nieuw bericht" if n == 1 else f"{n} nieuwe berichten")
    )
    if result.get("error"):
        return "warn", f"Nieuws ververst: {new}. Niet alles lukte; zie de melding op de pagina."
    if not result.get("assessing"):
        return "info", f"Nieuws ververst: {new} (niet beoordeeld: geen Claude-sleutel)."
    assessed = int(result.get("assessed") or 0)
    text = f"Nieuws ververst: {new}, {assessed} beoordeeld door Claude"
    pending = int(result.get("pending") or 0)
    if pending:
        text += f"; nog {pending} te beoordelen bij de volgende ronde"
    return "ok", text + "."


@app.get("/news", response_class=HTMLResponse, include_in_schema=False)
async def news_page(
    request: Request,
    hours: int = Query(NEWS_DEFAULT_HOURS, ge=1, le=NEWS_HOURS_MAX),
    # "ja" = only facts that weigh in the sentiment, "nee" = only the rest
    telt: str = Query("", max_length=8),
    page: int = Query(1, ge=1, le=10_000),
    news: NewsService = Depends(get_news),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    counted = {"ja": True, "nee": False}.get(telt)
    telt = telt if counted is not None else ""
    groups, n_articles, truncated = _news_groups(session, settings, hours)
    n_counted = sum(1 for g in groups if g.status.counts)
    shown = [g for g in groups if counted is None or g.status.counts == counted]
    pages = max(1, -(-len(shown) // NEWS_PAGE_SIZE))
    page = min(page, pages)
    first = (page - 1) * NEWS_PAGE_SIZE
    page_groups = shown[first : first + NEWS_PAGE_SIZE]

    # The coin cards link their top facts to the fact in the list of facts that count (48 hours,
    # the sentiment's own window), on the page where it stands.
    if hours == SENTIMENT_WINDOW_H and counted is True:
        counting = shown
    else:
        counting = [
            g
            for g in (
                groups
                if hours == SENTIMENT_WINDOW_H
                else _news_groups(session, settings, SENTIMENT_WINDOW_H)[0]
            )
            if g.status.counts
        ]
    where = {g.key: i for i, g in enumerate(counting)}
    sentiments = [market_sentiment(session, c, SENTIMENT_WINDOW_H) for c in settings.coins]
    for s in sentiments:
        for t in s.top:
            key = event_key_for(t)
            index = where.get(key)
            t.list_url = (  # type: ignore[attr-defined]
                _news_url(SENTIMENT_WINDOW_H, "ja", index // NEWS_PAGE_SIZE + 1, fact_anchor(key))
                if index is not None
                else None
            )
            t.lang = text_lang(t.title)  # type: ignore[attr-defined]
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "settings": settings,
            "hours": hours,
            "periods": NEWS_PERIODS,
            "telt": telt,
            "groups": page_groups,
            "n_total": len(groups),
            "n_counted": n_counted,
            "n_shown": len(shown),
            "n_articles": n_articles,
            "truncated": truncated,
            "max_items": NEWS_PAGE_MAX_ITEMS,
            "page": page,
            "pages": pages,
            "first": first + 1 if page_groups else 0,
            "last": first + len(page_groups),
            "newer_url": _news_url(hours, telt, page - 1, "berichten") if page > 1 else None,
            "older_url": _news_url(hours, telt, page + 1, "berichten") if page < pages else None,
            "filter_urls": {
                "": _news_url(hours, "", 1, "berichten"),
                "ja": _news_url(hours, "ja", 1, "berichten"),
                "nee": _news_url(hours, "nee", 1, "berichten"),
            },
            "period_urls": {h: _news_url(h, telt, 1, "berichten") for h, _ in NEWS_PERIODS},
            "sentiments": sentiments,
            "sentiment_hours": SENTIMENT_WINDOW_H,
            "market_mood": load_market_mood(session) if settings.fear_greed_enabled else None,
            "fear_greed_nl": FEAR_GREED_NL,
            "last_run": news.last_run,
            "last_error": news.last_error,
            "assessing": news.analyst.available,
            "analyst_available": news.analyst.available,
        },
    )


@app.post("/news/refresh", include_in_schema=False)
async def news_refresh_form(
    hours: int = Form(NEWS_DEFAULT_HOURS, ge=1, le=NEWS_HOURS_MAX),
    telt: str = Form("", max_length=8),
    news: NewsService = Depends(get_news),
    session: Session = Depends(get_session),
):
    result = await news.refresh(session)
    kind, text = _news_refresh_flash(result)
    back = _news_url(hours, telt if telt in ("ja", "nee") else "", 1, "berichten")
    return flash.redirect_with_flash(back, kind, text)


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
        "revisit": engine.revisit_status(),
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
        # Running news-shock pauses: what, since when, the bounds and Claude's last judgement.
        "shock_pauses": engine.shock.status(session),
        # The pauses of the last 30 days, running ones included: how long and how they ended.
        "shock_history": engine.shock.history(session),
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
        raise errors.bitvavo_api_error(exc) from exc


class ConsultIn(BaseModel):
    market: str = Field(min_length=5, max_length=16)


@app.post("/api/trading/consult")
async def trading_consult(
    body: ConsultIn,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """Ask Claude about one market now ('Claude laten beoordelen'), whatever the signal says.
    The rules still hold: without a buy signal he cannot buy."""
    market = body.market.strip().upper()
    if not engine.decision_maker.available:
        raise _claude_missing(display.CONSULT_NO_CLAUDE)
    try:
        return (await engine.run_consult(session, market)).as_dict()
    except ValueError as exc:  # not one of the bot's markets
        raise errors.ApiError(
            422, f"{exc}.", "market_not_tracked", fields={"market": "geen markt van de bot"}
        ) from exc
    except BudgetExceeded as exc:
        raise _budget_error(exc, "Beoordeling niet gestart: ") from exc
    except BitvavoError as exc:
        raise errors.bitvavo_api_error(exc) from exc


class PauseIn(BaseModel):
    paused: bool


@app.post("/api/trading/pause")
async def trading_pause(body: PauseIn, session: Session = Depends(get_session)):
    risk.set_paused(session, body.paused)
    return {"paused": body.paused}


@app.get("/api/positions")
async def positions(
    request: Request,
    response: Response,
    mode: Literal["paper", "live"] | None = Query(None),
    days: int = Query(30, ge=1, le=3650, description="Gesloten posities van de laatste N dagen"),
    limit: int = Query(200, ge=1, le=1000, description="Gesloten posities per pagina"),
    cursor: str | None = CURSOR_QUERY,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """Positions of the current mode, or of ``mode`` (e.g. the paper positions that the exits
    still guard while the bot trades live). ``open`` is always complete; ``closed`` (newest
    first) is paged: more via the Link header."""
    mode = mode or engine.mode
    closed = closed_positions(session, mode, days, limit + 1, before=time_id_cursor(cursor))
    closed = page_of(
        request,
        response,
        closed,
        limit,
        lambda p: encode_cursor(at=p.closed_at.isoformat(), id=p.id),
    )
    return {
        "mode": mode,
        "open": [_position_dict(p) for p in open_positions(session, mode)],
        "closed": [_position_dict(p) for p in closed],
    }


@app.get("/api/decisions")
async def decisions(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = CURSOR_QUERY,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """The newest decisions first; more via the Link header."""
    rows = recent_decisions(session, engine.mode, limit + 1, before_id=id_cursor(cursor))
    rows = page_of(request, response, rows, limit, lambda d: encode_cursor(id=d.id))
    return [_decision_dict(d) for d in rows]


@app.post("/api/evaluations", status_code=201)
async def run_evaluation(
    response: Response,
    days: int = Query(7, ge=1, le=365),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    if not engine.evaluator.available:
        raise _claude_missing()
    try:
        ev = await engine.run_evaluation(session, days)
    except EvaluationBusy as exc:
        raise errors.ApiError(409, str(exc), "evaluation_busy") from exc
    except BudgetExceeded as exc:
        raise _budget_error(exc) from exc
    except BitvavoError as exc:
        raise errors.bitvavo_api_error(exc) from exc
    except RuntimeError as exc:
        raise errors.ApiError(502, str(exc), "claude_error") from exc
    response.headers["Location"] = f"/api/evaluations/{ev.id}"
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
async def list_evaluations(
    request: Request,
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = CURSOR_QUERY,
    session: Session = Depends(get_session),
):
    """The newest evaluations first; more via the Link header."""
    stmt = select(Evaluation)
    before = id_cursor(cursor)
    if before is not None:
        stmt = stmt.where(Evaluation.id < before)
    rows = list(session.scalars(stmt.order_by(Evaluation.id.desc()).limit(limit + 1)))
    rows = page_of(request, response, rows, limit, lambda e: encode_cursor(id=e.id))
    return [_evaluation_dict(e) for e in rows]


@app.get("/api/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: int = Path(ge=1, le=DB_ID_MAX), session: Session = Depends(get_session)
):
    ev = session.get(Evaluation, evaluation_id)
    if ev is None:
        raise errors.ApiError(
            404, f"Evaluatie {evaluation_id} bestaat niet.", "evaluation_not_found"
        )
    return _evaluation_dict(ev)


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
    return "geen stop" if value is None else limit_pct(value)


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
            f"het voorstel geeft live op {limit_pct(live_pct)} van de candles een koopsignaal "
            f"(meer dan {limit_pct(PROPOSAL_MAX_LIVE_BUY_PCT)}); pas dat zelf aan op de "
            "strategiepagina als je dat wilt"
        )
    return problems


def _proposal_problems(ev: Evaluation, current: Strategy, settings: Settings) -> list[str]:
    """Why an evaluation's proposal cannot be applied as it is (empty: it can)."""
    if not ev.proposed_strategy:
        return []
    proposal = _proposal(ev)
    if proposal is None:
        return ["het voorstel is geen geldige strategie"]
    problems = use_problems(proposal)
    if proposal.market not in settings.markets:
        problems.append(f"{proposal.market} staat niet in MARKETS")
    return problems + _proposal_guard_problems(current, proposal, ev)


# The strategy that was active before each applied proposal (newest last), so the page can show
# what an applied proposal changed and the owner can put the previous strategy back.
APPLIED_PROPOSALS_KEY = "strategy:applied_proposals"
APPLIED_PROPOSALS_KEEP = 10


def _applied_history(session: Session) -> list[dict]:
    row = session.get(Setting, APPLIED_PROPOSALS_KEY)
    if row is None:
        return []
    try:
        data = json.loads(row.value)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("evaluation_id"), int)]


def _belongs_to(entry: dict | None, ev: Evaluation | None) -> bool:
    """Whether a history entry is about this evaluation (same id and the same moment, so an
    entry never sticks to a new evaluation that reuses the id of a deleted one)."""
    if entry is None or ev is None or entry.get("evaluation_id") != ev.id or not ev.created_at:
        return False
    created = _parse_moment(entry.get("evaluation_at"))
    own = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=UTC)
    return created is not None and abs((created - own).total_seconds()) < 1


def _store_applied_history(session: Session, entries: list[dict]) -> None:
    """Stage the history in the session; the caller commits."""
    payload = json.dumps(entries[-APPLIED_PROPOSALS_KEEP:], ensure_ascii=False)
    row = session.get(Setting, APPLIED_PROPOSALS_KEY)
    if row is None:
        session.add(Setting(key=APPLIED_PROPOSALS_KEY, value=payload))
    else:
        row.value = payload


def _previous_strategy(entry: dict | None) -> Strategy | None:
    if not entry or not isinstance(entry.get("previous"), dict):
        return None
    try:
        return Strategy.model_validate(entry["previous"])
    except ValidationError:
        return None


def _apply_proposal(request: Request, session: Session, evaluation_id: int) -> Strategy:
    """Make an evaluation's proposal the active strategy, with the same checks as a save via
    the API or the strategy page. The strategy it replaces is kept, so it can be put back.
    Raises ProposalError."""
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
    current, load_warning = load_strategy_checked(session)
    guard = _proposal_guard_problems(current, strategy, ev)
    if guard:
        message = "; ".join(guard)
        raise ProposalError(400, message[0].upper() + message[1:] + ".")
    history = _applied_history(session)
    history.append(
        {
            "evaluation_id": ev.id,
            "evaluation_at": ev.created_at.isoformat() if ev.created_at else None,
            "applied_at": datetime.now(UTC).isoformat(),
            # An unusable stored strategy is not offered back (the default stood in for it).
            "previous": None if load_warning else current.model_dump(mode="json"),
            "reverted_at": None,
        }
    )
    _store_applied_history(session, history)
    ev.applied = True
    save_strategy(session, strategy)  # commits the strategy, the history and ev.applied
    return strategy


def _revert_proposal(request: Request, session: Session, evaluation_id: int) -> Strategy:
    """Put back the strategy that was active before this evaluation's proposal was applied.
    Only for the most recently applied proposal, and only while it is still the active
    strategy: an older tab must not undo a later change. Raises ProposalError."""
    ev = session.get(Evaluation, evaluation_id)
    history = _applied_history(session)
    entry = history[-1] if history else None
    if ev is None or not ev.applied or not _belongs_to(entry, ev):
        raise ProposalError(
            409, "Terugzetten kan alleen bij het laatst toegepaste voorstel van Claude."
        )
    if entry.get("reverted_at"):
        raise ProposalError(409, "De vorige strategie is al teruggezet.")
    previous = _previous_strategy(entry)
    if previous is None:
        raise ProposalError(409, "De vorige strategie is niet bewaard en kan niet terug.")
    proposal = _proposal(ev)
    if proposal is None or load_strategy(session).model_dump() != proposal.model_dump():
        raise ProposalError(
            409,
            "De strategie is na het toepassen nog gewijzigd; terugzetten zou die wijziging "
            "ongedaan maken. Pas de strategie zo nodig aan op de strategiepagina.",
        )
    if previous.market not in request.app.state.settings.markets:
        raise ProposalError(400, f"{previous.market} staat niet meer in MARKETS.")
    try:
        check_for_use(previous)
    except ValueError as exc:
        raise ProposalError(400, str(exc)) from exc
    entry["reverted_at"] = datetime.now(UTC).isoformat()
    _store_applied_history(session, history)
    save_strategy(session, previous)  # commits both
    return previous


@app.post("/api/evaluations/{evaluation_id}/apply")
async def apply_evaluation(
    request: Request,
    evaluation_id: int = Path(ge=1, le=DB_ID_MAX),
    session: Session = Depends(get_session),
):
    try:
        strategy = _apply_proposal(request, session, evaluation_id)
    except ProposalError as exc:
        # The pages use 400 for 'cannot be applied'; the API says 422 (refused by the rules).
        status = 422 if exc.status == 400 else exc.status
        code = {404: "evaluation_not_found", 409: "proposal_conflict"}.get(
            status, "proposal_not_applicable"
        )
        raise errors.ApiError(status, exc.message, code) from exc
    return {"applied": True, "strategy": strategy.model_dump()}


def _evaluation_rows(session: Session, settings: Settings, current: Strategy) -> list[dict]:
    """The last evaluations for the page: what applying a proposal would change (or, once
    applied, what it changed), why it cannot be applied, and whether it can be undone."""
    evaluations = session.scalars(select(Evaluation).order_by(Evaluation.id.desc()).limit(5)).all()
    applied = _applied_history(session)
    latest = applied[-1] if applied else None
    rows = []
    for e in evaluations:
        row = _evaluation_dict(e)
        row["model_nl"] = display.model_name_nl(e.model)
        proposal = _proposal(e)
        row["proposal_name"] = proposal.name if proposal is not None else None
        row["problems"] = _proposal_problems(e, current, settings)
        row["diff"] = None
        row["applied_at"] = row["reverted_at"] = row["previous_name"] = None
        row["can_revert"] = row["revert_blocked"] = False
        if proposal is not None and not e.applied:
            # What applying would change.
            row["diff"] = strategy_diff(current, proposal)
        elif proposal is not None:
            entry = next((h for h in reversed(applied) if _belongs_to(h, e)), None)
            previous = _previous_strategy(entry)
            if entry is not None:
                row["applied_at"] = _parse_moment(entry.get("applied_at"))
                row["reverted_at"] = _parse_moment(entry.get("reverted_at"))
            if previous is not None:
                # What it changed, compared with the strategy it replaced.
                row["diff"] = strategy_diff(previous, proposal)
                row["previous_name"] = previous.name
                if entry is latest and not entry.get("reverted_at"):
                    still_active = current.model_dump() == proposal.model_dump()
                    row["can_revert"] = still_active
                    row["revert_blocked"] = not still_active
        rows.append(row)
    return rows


def _parse_moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# How long a just applied proposal stays at the top of Handelen (with its undo button).
RECENTLY_APPLIED = timedelta(hours=24)


_BITVAVO_ERROR_RE = re.compile(r"^Bitvavo (\d+) \(code ([^)]*)\): (.*)$", re.S)


def _cycle_failure_nl(reason: str | None) -> str:
    """Why one market's part of a cycle failed, in words (the engine keeps the exception text:
    'Bitvavo 400 (code 205): ...')."""
    text = str(reason or "").strip()
    m = _BITVAVO_ERROR_RE.match(text)
    if not m:
        return text or "onbekend"
    status, code, message = int(m[1]), m[2], m[3].strip().rstrip(".")
    if status == 0:
        return "Bitvavo reageert niet (geen verbinding of een time-out)"
    if status >= 500:
        return f"Bitvavo had een storing (fout {status})"
    detail = f"fout {status}" + (f", code {code}" if code and code != "None" else "")
    return f"Bitvavo weigerde het verzoek ({detail}: {message})"


# How far back Handelen judges the blocked buys: long enough for a verdict on a rule, not so
# long that the table dwarfs the agenda.
BLOCKED_REVIEW_DAYS = 30


def _trading_context(
    request: Request,
    engine: TradingEngine,
    session: Session,
    *,
    view: str = "important",
    market: str | None = None,
    page: int = 1,
) -> dict:
    settings: Settings = request.app.state.settings
    strategy = load_strategy(session)
    labels = display.indicator_labels(strategy)
    last_cycle = latest_decisions(session, engine.mode, list(engine.markets))
    evaluation_rows = _evaluation_rows(session, settings, strategy)
    now = datetime.now(UTC)
    # At the top of the page: the newest proposal that waits for the owner, or else the one
    # that was just applied (so its changes and the way back are where the owner looks).
    featured = next(
        (r for r in evaluation_rows if r["proposed_strategy"] and not r["applied"]),
        None,
    )
    if featured is not None and featured["problems"]:
        featured = None  # cannot be applied: shown with the reason under Evaluaties only
    if featured is None:
        featured = next(
            (
                r
                for r in evaluation_rows
                if r["can_revert"] and r["applied_at"] and now - r["applied_at"] < RECENTLY_APPLIED
            ),
            None,
        )
    status = _trading_status(engine, session, settings)
    other = "paper" if engine.mode == "live" else "live"
    # Under Gesloten posities: closed trades of the other mode only (its open positions are
    # listed under Open posities already, UX-01).
    other_count = mode_counts(session, "closed").get(other, 0)
    # The dip watch journals its conditions with indicator ids ('rsi14 stijgt'): readable names.
    status["watch"] = [
        {
            **w,
            "dip": display.readable_conditions(w.get("dip"), labels),
            "waiting_for": display.readable_conditions(w.get("waiting_for"), labels),
        }
        for w in status.get("watch") or []
    ]
    figures = risk_now(engine, session, settings)
    prices = engine.last_prices or {}
    claude_ok = engine.evaluator.available
    next_evaluation = None
    if claude_ok and settings.trading_enabled and settings.evaluation_interval_hours > 0:
        last_eval = _load_last_run("evaluation")
        if last_eval is not None:
            next_evaluation = max(
                last_eval + timedelta(hours=settings.evaluation_interval_hours), now
            )
    return {
        "settings": settings,
        "status": status,
        "markets": list(engine.markets),
        "other_mode": {"mode": other, "count": other_count} if other_count else None,
        # Under Agenda en risk-off: what the buys the rules blocked would have done.
        "blocked_review": blocked_buy_review(
            session, engine.mode, now - timedelta(days=BLOCKED_REVIEW_DAYS), now, prices
        ),
        "blocked_review_days": BLOCKED_REVIEW_DAYS,
        # One status for the hero (app/trading/health.py) and the limits as current figures.
        "health": _health(request, engine, session, figures=figures, paused=status["paused"]),
        "risk_now": figures,
        "positions": [
            display.position_view(p, prices.get(p.market), engine.fee_pct)
            for p in open_positions(session, engine.mode)
        ],
        # UX-01: the other mode's open positions are still guarded by the exits.
        "other_positions": [
            display.position_view(p, prices.get(p.market), engine.fee_pct)
            for p in open_positions(session, other)
        ],
        "prices_at": engine.last_prices_at if prices else None,
        "closed_positions": closed_positions(session, engine.mode)[:20],
        "cycles": display.cycle_log(
            session,
            engine.mode,
            days=settings.decision_history_days,
            view=view,
            market=market,
            page=page,
            markets=list(engine.markets),
            labels=labels,
        ),
        "last_cycle": [display.decision_view(d, labels) for d in last_cycle],
        # Markets whose part of the last cycle failed (engine.run_cycle keeps the others going).
        "failed_markets": [
            {
                "market": r.get("market"),
                "reason": _cycle_failure_nl(r.get("reason")),
                "at": engine.last_run,
            }
            for r in engine.last_results or []
            if r.get("signal") == "error" and r.get("market") in engine.markets
        ],
        "levels": {d.id: levels_summary(strategy, d.indicator_values()) for d in last_cycle},
        "evaluations": evaluation_rows,
        "featured": featured,
        "strategy_name": strategy.name,
        "evaluation_model_nl": display.model_name_nl(settings.claude_model_evaluation),
        "evaluation_available": claude_ok,
        "next_evaluation": next_evaluation,
    }


@app.get("/trading", response_class=HTMLResponse, include_in_schema=False)
async def trading_page(
    request: Request,
    view: Literal["important", "all"] = Query("important"),
    market: str | None = Query(None, max_length=16, pattern=r"^[A-Z0-9]{1,15}-[A-Z]{2,5}$"),
    page: int = Query(1, ge=1, le=100_000),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        request,
        "trading.html",
        _trading_context(request, engine, session, view=view, market=market, page=page),
    )


APPLY_ACTION_RE = re.compile(r"apply:([0-9]{1,9})")
REVERT_ACTION_RE = re.compile(r"revert:([0-9]{1,9})")
CONSULT_ACTION_RE = re.compile(r"consult:([A-Z0-9]{2,10}-EUR)")
# Where /trading/action may send the browser back to (the hidden field "next").
TRADING_ACTION_NEXT = ("/", "/trading")


def _trading_next(value: str) -> str:
    """Overzicht and Handelen only; no value means Handelen, anything else Overzicht."""
    if not value:
        return "/trading"
    return value if value in TRADING_ACTION_NEXT else "/"


def _action_refused(request: Request, back: str, status: int, text: str) -> RedirectResponse:
    """A refused action: back to the page with the reason (browsers), or the status code with
    the reason as detail (scripts and API clients)."""
    if errors.wants_html(request):
        return flash.redirect_with_flash(back, "error", text)
    raise HTTPException(status, text)


@app.post("/trading/action", include_in_schema=False)
async def trading_action(
    request: Request,
    action: str = Form(..., max_length=32),
    next_url: str = Form("", alias="next", max_length=512),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """Kill switch, cycle, evaluation and proposals. Every outcome is a redirect with a message
    (Post/Redirect/Get). ``next`` (the kill-switch macro, Overzicht) is the page to return to:
    "/" or "/trading"."""
    back = _trading_next(next_url)
    if action == "pause":
        risk.set_paused(session, True)
        return flash.redirect_with_flash(back, "ok", display.PAUSED_FLASH)
    if action == "resume":
        risk.set_paused(session, False)
        return flash.redirect_with_flash(back, "ok", display.RESUMED_FLASH)
    if action == "cycle":
        try:
            results = await engine.run_cycle(session)
        except BitvavoError as exc:
            if not errors.wants_html(request):
                raise
            text = errors.bitvavo_text(exc)
            return flash.redirect_with_flash(
                back,
                "error",
                f"Cyclus niet gedraaid: {text} De automatische cyclus probeert het later opnieuw.",
            )
        kind, text = display.cycle_flash(results)
        return flash.redirect_with_flash(back, kind, text)
    if action == "evaluate":
        if not engine.evaluator.available:
            return _action_refused(
                request,
                "/trading",
                409,
                "Evalueren kan niet: de Claude-sleutel ontbreekt (ANTHROPIC_API_KEY in de "
                "serveromgeving).",
            )
        try:
            ev = await engine.run_evaluation(session)
        except EvaluationBusy as exc:
            return _action_refused(request, "/trading", 409, str(exc))
        except BudgetExceeded as exc:
            return _action_refused(request, "/trading", 429, f"Evaluatie niet gestart: {exc}")
        except RuntimeError as exc:
            return _action_refused(request, "/trading", 502, f"Evaluatie mislukt: {exc}")
        proposal = _proposal(ev)
        if proposal is None:
            text = "Evaluatie klaar. Claude stelt deze keer geen andere strategie voor."
        elif _proposal_problems(ev, load_strategy(session), request.app.state.settings):
            text = (
                f"Evaluatie klaar. Claude stelt de strategie '{proposal.name}' voor, maar die "
                "kan zo niet worden toegepast; de reden staat bij Evaluaties."
            )
        else:
            text = (
                f"Evaluatie klaar. Claude stelt de strategie '{proposal.name}' voor; het "
                "voorstel staat bovenaan deze pagina."
            )
        return flash.redirect_with_flash("/trading", "ok", text)
    if action.startswith("consult:"):
        to_consult = CONSULT_ACTION_RE.fullmatch("consult:" + action[8:].upper())
        if to_consult is None:
            raise HTTPException(400, "Ongeldige markt: verwacht consult:<MUNT>-EUR.")
        market = to_consult.group(1)
        if not engine.decision_maker.available:
            return _action_refused(request, "/trading", 409, display.CONSULT_NO_CLAUDE)
        try:
            result = await engine.run_consult(session, market)
        except ValueError as exc:
            return _action_refused(request, "/trading", 404, f"{exc}.")
        except BudgetExceeded as exc:
            return _action_refused(request, "/trading", 429, f"Beoordeling niet gestart: {exc}")
        except BitvavoError as exc:
            if not errors.wants_html(request):
                raise
            return _action_refused(
                request, "/trading", 502, f"Beoordeling niet gedaan: {errors.bitvavo_text(exc)}"
            )
        kind, text = display.consult_flash(result)
        return flash.redirect_with_flash("/trading", kind, text)
    to_apply = APPLY_ACTION_RE.fullmatch(action)
    to_revert = REVERT_ACTION_RE.fullmatch(action)
    if action.startswith("apply:") and to_apply is None:
        raise HTTPException(400, "Ongeldig voorstel: verwacht apply:<nummer>.")
    if action.startswith("revert:") and to_revert is None:
        raise HTTPException(400, "Ongeldig voorstel: verwacht revert:<nummer>.")
    if to_apply is None and to_revert is None:
        raise HTTPException(400, "Onbekende actie.")
    try:
        if to_apply is not None:
            strategy = _apply_proposal(request, session, int(to_apply.group(1)))
            text = f"Strategie '{strategy.name}' is nu actief."
        else:
            strategy = _revert_proposal(request, session, int(to_revert.group(1)))
            text = f"De vorige strategie '{strategy.name}' is weer actief."
    except ProposalError as exc:
        return _action_refused(request, "/trading", exc.status, exc.message)
    text += " Open posities houden hun huidige stop-loss en take-profit."
    if strategy.trailing_stop_pct and open_positions(session, engine.mode):
        text += " De trailing stop schuift voortaan mee volgens deze strategie."
    return flash.redirect_with_flash("/trading", "ok", text)


# -- Trading plan ---------------------------------------------------------------------


def _plan_claude(settings: Settings, available: bool, budget: dict) -> dict:
    """Whether Claude applies the plan right now, in one honest sentence (UX-11): the owner's
    rules are instructions for Claude, so without Claude they are not applied at all."""
    if available and not budget.get("exhausted"):
        return {
            "active": True,
            "text": "Claude leest dit plan bij elke beslissing en elke evaluatie.",
        }
    why = "er is geen Claude-sleutel" if not available else "het Claude-budget van deze maand is op"
    if settings.claude_unavailable_mode == "indicators":
        text = (
            f"Claude is nu niet beschikbaar ({why}). De bot volgt dan alleen het koopsignaal van "
            "de strategie, met de halve inzet: je eigen regels hieronder worden nu niet "
            "toegepast, ook niet een regel als 'niet kopen in het weekend'."
        )
    else:
        text = (
            f"Claude is nu niet beschikbaar ({why}). De bot koopt daarom niets tot Claude terug "
            "is; een verkoopsignaal volgt hij wel. Je plan wordt nu dus niet gebruikt."
        )
    return {"active": False, "text": text}


def _plan_context(request: Request, engine: TradingEngine, session: Session) -> dict:
    """Everything the plan page shows: the owner's own text plus the rules the code follows."""
    settings: Settings = request.app.state.settings
    strategy = load_strategy(session)
    budget = request.app.state.budget.status(session)
    return {
        "settings": settings,
        "plan": load_plan(session),
        "fields": PLAN_FIELDS,
        "max_length": PLAN_MAX_LENGTH,
        "strategy": strategy,
        # Indicator ids as people read them ('rsi14' -> 'RSI 14'), as on Handelen.
        "indicator_labels": display.indicator_labels(strategy),
        "mode": engine.mode,
        "limits": engine.limits,
        "fee_pct": engine.fee_pct,
        "news_entry_min_score": NEWS_ENTRY_MIN_SCORE,
        "news_exit_max_score": NEWS_EXIT_MAX_SCORE,
        "half_order_pct": int(NEWS_ENTRY_SIZE_FACTOR * 100),
        "budget": budget,
        "paused": risk.is_paused(session),
        "claude_available": engine.decision_maker.available,
        "claude": _plan_claude(settings, engine.decision_maker.available, budget),
        "news_min_sources": NEWS_MIN_SOURCES,
        "error": None,
        "field_errors": {},
    }


@app.get("/api/plan")
async def api_plan(session: Session = Depends(get_session)):
    return load_plan(session).model_dump()


@app.put("/api/plan")
async def api_put_plan(plan: TradingPlan, session: Session = Depends(get_session)):
    return save_plan(session, plan).model_dump()


PLAN_SAVED = "Tradingplan opgeslagen. Claude krijgt het vanaf de volgende beslissing mee."
# Without Claude the plan is stored but not applied: the message must not promise otherwise.
PLAN_SAVED_IDLE = (
    "Tradingplan opgeslagen. Claude is nu niet beschikbaar, dus de bot gebruikt het plan pas "
    "als Claude terug is."
)


@app.get("/plan", response_class=HTMLResponse, include_in_schema=False)
async def plan_page(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    ctx = _plan_context(request, engine, session)
    return templates.TemplateResponse(request, "plan.html", ctx)


@app.post("/plan", response_class=HTMLResponse, include_in_schema=False)
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
        errors: dict[str, str] = {}
        for e in exc.errors():
            key = str(e["loc"][0]) if e.get("loc") else ""
            if key in PLAN_MAX_LENGTH and str(e.get("type", "")).startswith("string_too_long"):
                errors[key] = (
                    f"te lang: {len(raw[key])} tekens, maximaal {PLAN_MAX_LENGTH[key]}. "
                    "Kort de tekst in."
                )
            else:
                errors[key] = "deze tekst kan niet worden opgeslagen"
        ctx["field_errors"] = errors
        ctx["error"] = "Niet opgeslagen: " + "; ".join(
            f"{PLAN_FIELDS.get(key, (key,))[0]} is {msg}" for key, msg in errors.items()
        )
        return templates.TemplateResponse(request, "plan.html", ctx, status_code=400)
    save_plan(session, plan)
    # A one-time message (not ?saved=1): a reload or a restored tab shows no stale "saved". It
    # only promises that Claude uses the plan when Claude is there to use it.
    claude = _plan_claude(
        request.app.state.settings,
        engine.decision_maker.available,
        request.app.state.budget.status(session),
    )
    return flash.redirect_with_flash(
        "/plan#eigen", "ok", PLAN_SAVED if claude["active"] else PLAN_SAVED_IDLE
    )


# -- Trade journal --------------------------------------------------------------------

JOURNAL_STATUS_NL = {"open": "open", "closed": "gesloten"}
JOURNAL_RESULT_NL = {"win": "winst", "loss": "verlies"}
JOURNAL_BY_NL = {"bot": "de bot", "manual": "jij, handmatig"}
JOURNAL_DAYS = (7, 30, 90, 365)


def _journal_filter(
    market: str | None,
    status: str | None,
    result: str | None,
    reason: str | None,
    tag: str | None,
    days: int | None,
    q: str | None,
    by: str | None = None,
) -> tuple[JournalFilter, list[str]]:
    """The filter, plus a Dutch line per value that was not understood and is ignored (a link
    with ?reason=xyz must not silently show the unfiltered list as if it were filtered)."""
    ignored: list[str] = []

    def pick(value: str | None, allowed, label: str) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if value in allowed:
            return value
        ignored.append(f"{label} '{value}'")
        return None

    flt = JournalFilter(
        market=(market or "").strip().upper() or None,
        status=pick(status, JOURNAL_STATUS_NL, "status"),
        result=pick(result, JOURNAL_RESULT_NL, "resultaat"),
        reason=pick(reason, EXIT_NL, "hoe gesloten"),
        tag=(tag or "").strip() or None,
        days=days if days and days > 0 else None,
        query=(q or "").strip() or None,
        opened_by=pick(by, JOURNAL_BY_NL, "geopend door"),
    )
    return flt, ignored


def _blank_to_none(value: object) -> object:
    """An empty form field ('days=') means 'no filter', not an invalid number."""
    return None if isinstance(value, str) and not value.strip() else value


# Journal filters: free text is short, days stay within what timedelta and the database
# handle. An empty 'days' (the 'alles' option of older pages) is no filter.
JournalDays = Annotated[
    Annotated[int, Field(ge=0, le=3650)] | None, BeforeValidator(_blank_to_none), Query()
]
JOURNAL_FILTERS = {
    "market": Query(None, max_length=16),
    "status": Query(None, max_length=16),
    "result": Query(None, max_length=16),
    "reason": Query(None, max_length=32),
    "tag": Query(None, max_length=64),
    "q": Query(None, max_length=200),
    "by": Query(None, max_length=16),
    "mode": Query(None, max_length=8),
}


def _journal_mode(engine: TradingEngine, mode: str | None) -> str:
    return mode if mode in ("paper", "live") else engine.mode


def _journal_params(flt: JournalFilter, mode: str, engine_mode: str) -> dict[str, str]:
    """The query parameters of a journal view, in a fixed order (the same URL every time)."""
    params = {
        "mode": mode if mode != engine_mode else "",
        "market": flt.market or "",
        "status": flt.status or "",
        "result": flt.result or "",
        "reason": flt.reason or "",
        "by": flt.opened_by or "",
        "days": str(flt.days) if flt.days else "",
        "tag": flt.tag or "",
        "q": flt.query or "",
    }
    return {k: v for k, v in params.items() if v}


def _journal_url(params: dict[str, str], **changes: str) -> str:
    merged = {k: v for k, v in {**params, **changes}.items() if v}
    return "/journal" + (f"?{urlencode(merged)}" if merged else "")


def _journal_chips(flt: JournalFilter, params: dict[str, str]) -> list[dict[str, str]]:
    """The active filters as removable labels ('Markt: ETH-EUR ×' -> the view without it)."""
    chips = []

    def add(key: str, text: str) -> None:
        chips.append({"text": text, "href": _journal_url(params, **{key: ""})})

    if flt.market:
        add("market", f"Markt: {flt.market}")
    if flt.status:
        add("status", f"Status: {JOURNAL_STATUS_NL[flt.status]}")
    if flt.result:
        add("result", f"Resultaat: {JOURNAL_RESULT_NL[flt.result]}")
    if flt.reason:
        add("reason", f"Hoe gesloten: {EXIT_NL[flt.reason]}")
    if flt.opened_by:
        add("by", f"Geopend door: {JOURNAL_BY_NL[flt.opened_by]}")
    if flt.days:
        add("days", f"Periode: laatste {flt.days} dagen")
    if flt.tag:
        add("tag", f"Label: {flt.tag}")
    if flt.query:
        add("q", f"Zoekterm: '{flt.query}'")
    return chips


@app.get("/api/journal")
async def api_journal(
    market: str | None = JOURNAL_FILTERS["market"],
    status: str | None = JOURNAL_FILTERS["status"],
    result: str | None = JOURNAL_FILTERS["result"],
    reason: str | None = JOURNAL_FILTERS["reason"],
    tag: str | None = JOURNAL_FILTERS["tag"],
    days: JournalDays = None,
    q: str | None = JOURNAL_FILTERS["q"],
    by: str | None = JOURNAL_FILTERS["by"],
    mode: str | None = JOURNAL_FILTERS["mode"],
    limit: int = Query(200, ge=1, le=1000),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    flt, ignored = _journal_filter(market, status, result, reason, tag, days, q, by)
    shown = _journal_mode(engine, mode)
    entries, truncated = journal_entries_page(session, shown, flt, limit)
    return {
        "mode": shown,
        "ignored_filters": ignored,
        # True when more positions matched than ``limit``: the oldest were left out (and the
        # totals cover only the entries shown). Narrow the filter or raise ``limit``.
        "truncated": truncated,
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
        raise errors.ApiError(404, str(exc), "position_not_found") from exc
    except ValueError as exc:
        raise errors.ApiError(422, str(exc), "invalid_note") from exc
    entry = journal_entry(session, position_id)
    assert entry is not None
    return entry.as_dict()


@app.get("/journal", response_class=HTMLResponse, include_in_schema=False)
async def journal_page(
    request: Request,
    market: str | None = JOURNAL_FILTERS["market"],
    status: str | None = JOURNAL_FILTERS["status"],
    result: str | None = JOURNAL_FILTERS["result"],
    reason: str | None = JOURNAL_FILTERS["reason"],
    tag: str | None = JOURNAL_FILTERS["tag"],
    days: JournalDays = None,
    q: str | None = JOURNAL_FILTERS["q"],
    by: str | None = JOURNAL_FILTERS["by"],
    mode: str | None = JOURNAL_FILTERS["mode"],
    saved: int | None = Query(None, ge=0, le=DB_ID_MAX),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    flt, ignored = _journal_filter(market, status, result, reason, tag, days, q, by)
    if mode and mode not in ("paper", "live"):
        ignored.append(f"modus '{mode}'")
    shown = _journal_mode(engine, mode)
    entries = journal_entries(session, shown, flt)
    params = _journal_params(flt, shown, engine.mode)
    counts = mode_counts(session)
    other = "live" if shown == "paper" else "paper"
    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "settings": settings,
            "mode": shown,  # the mode whose trades are shown
            "engine_mode": engine.mode,  # the mode the bot trades in now
            "mode_counts": counts,
            # Offer the other mode only when it has trades (or is the one being shown).
            "mode_links": [
                {
                    "mode": m,
                    "count": counts[m],
                    "href": _journal_url(params, mode=m if m != engine.mode else ""),
                }
                for m in ("paper", "live")
            ]
            if counts[other] or shown != engine.mode
            else [],
            "filter": flt,
            "filter_count": len(_journal_chips(flt, params)),
            "chips": _journal_chips(flt, params),
            "ignored": ignored,
            "entries": entries,
            "totals": journal_totals(entries),
            "tags": all_tags(session, shown),
            "exit_reasons": EXIT_NL,
            "by_nl": JOURNAL_SOURCES_NL,
            "days_options": JOURNAL_DAYS,
            "other_orders": unlinked_orders(session, shown) if not flt.active else [],
            "saved": saved,
            # Where the note form returns to: this view with its filters, minus "saved".
            "back_url": _journal_url(params),
        },
    )


@app.post("/journal/{position_id}/note", include_in_schema=False)
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
SETTINGS_BLOCKS = (*cfg.GROUPS, *cfg.OWN_FORM_PARTS, cfg.PAPER_GROUP, "omgeving")


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
            "missing_nl": "de login staat uit",
            "fix": "zet hem op true",
            "env": "AUTH_ENABLED",
        },
        {
            "key": "bitvavo_key",
            "ok": client.authenticated,
            "label": "Er is een Bitvavo-sleutel",
            "missing": "er is geen Bitvavo-sleutel (BITVAVO_API_KEY en BITVAVO_API_SECRET)",
            "missing_nl": "er is geen Bitvavo-sleutel",
            "fix": "maak er bij Bitvavo een die mag lezen en handelen, niet opnemen",
            "env": "BITVAVO_API_KEY, BITVAVO_API_SECRET",
        },
        {
            "key": "ack",
            "ok": settings.live_trading_ack,
            "label": "Live handelen is op de server bevestigd",
            "missing": "LIVE_TRADING_ACK staat niet op true in de serveromgeving",
            "missing_nl": "live handelen is nog niet bevestigd op de server",
            "fix": "zet de bevestiging op true",
            "env": "LIVE_TRADING_ACK",
        },
    ]


def _live_missing_text(env: Settings, client: BitvavoClient) -> str | None:
    missing = [r["missing"] for r in live_requirements(env, client) if not r["ok"]]
    return "; ".join(missing) or None


# Bitvavo error codes for a refused key, in plain Dutch (the code itself is shown as detail).
KEY_PROBLEMS_NL = errors.KEY_PROBLEMS_NL


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
# 'rsi14 lager dan 35' -> 'RSI 14 lager dan 35' (a page passes the labels of its strategy).
templates.env.filters["readable_condition"] = display.readable_condition
templates.env.filters["model_name"] = display.model_name_nl
templates.env.filters["indicator_labels"] = display.indicator_labels


async def fetch_market_names(client: BitvavoClient) -> set[str] | None:
    """Every market Bitvavo lists, or None when the list cannot be fetched quickly."""
    try:
        rows = await asyncio.wait_for(client.markets(), MARKET_LIST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - offline or slow: the save goes on unchecked
        log.info("market list not available for the settings check: %s", exc)
        _note_outage(client, exc)
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


async def _api_password(request: Request) -> str | None:
    """The current password of a call without a settings body (DELETE): the JSON field
    current_password, or the X-Current-Password header (DELETE usually has no body)."""
    body = None
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            raise errors.ApiError(400, "Ongeldige JSON.", "invalid_json") from None
    value = body.get("current_password") if isinstance(body, dict) else None
    if value is not None and not isinstance(value, str):
        raise errors.ApiError(
            422,
            "Niet verwerkt: current_password moet tekst zijn.",
            "invalid_input",
            fields={"current_password": "moet tekst zijn"},
        )
    return value or request.headers.get(PASSWORD_HEADER)


class SettingsIn(BaseModel):
    """Setting -> new value, with the keys of GET /api/settings. Only the keys sent change."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"examples": [{"max_trade_eur": 50, "max_trades_per_day": 4}]},
    )
    current_password: str | None = Field(
        default=None,
        max_length=1024,
        description="Nodig als er een gevoelige instelling verandert (of de header "
        "X-Current-Password)",
    )


def _settings_refused(exc: SettingsError, status: int, code: str) -> errors.ApiError:
    fields = {str(k): str(v) for k, v in exc.errors.items()}
    return errors.ApiError(status, errors.fields_sentence(fields), code, fields=fields)


@app.get("/api/settings")
async def api_settings(request: Request, session: Session = Depends(get_session)):
    return _settings_payload(request, session)


@app.patch("/api/settings")
async def api_update_settings(
    body: SettingsIn, request: Request, session: Session = Depends(get_session)
):
    """Change settings: only the keys sent change. Sensitive settings need the current
    password (``current_password`` or the X-Current-Password header: step-up)."""
    password = body.current_password or request.headers.get(PASSWORD_HEADER)
    try:
        await _save_settings(request, session, dict(body.model_extra or {}), password)
    except SettingsError as exc:
        raise _settings_refused(exc, 422, "invalid_settings") from exc
    except StepUpError as exc:
        raise _step_up_error(exc) from exc
    return _settings_payload(request, session)


@app.put("/api/settings", deprecated=True)
async def api_put_settings(
    body: SettingsIn, request: Request, session: Session = Depends(get_session)
):
    """The same as PATCH (only the keys sent change); kept for existing clients."""
    return await api_update_settings(body, request, session)


@app.delete("/api/settings")
async def api_reset_settings(request: Request, session: Session = Depends(get_session)):
    """Forget every value saved in the portal; the environment applies again."""
    password = await _api_password(request)
    try:
        await _reset_settings(request, session, None, password)
    except SettingsError as exc:
        raise _settings_refused(exc, 422, "reset_refused") from exc
    except StepUpError as exc:
        raise _step_up_error(exc) from exc
    return _settings_payload(request, session)


@app.delete("/api/settings/{key}")
async def api_reset_setting(key: str, request: Request, session: Session = Depends(get_session)):
    if key not in cfg.SPEC_BY_KEY:
        raise errors.ApiError(404, f"{key} is geen aanpasbare instelling.", "unknown_setting")
    password = await _api_password(request)
    try:
        await _reset_settings(request, session, [key], password)
    except SettingsError as exc:
        raise _settings_refused(exc, 422, "reset_refused") from exc
    except StepUpError as exc:
        raise _step_up_error(exc) from exc
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


def _live_context(
    request: Request, settings: Settings, key_state: dict, paper_open: int = 0
) -> dict:
    """The checklist and confirmation panel for switching to live. ``paper_open`` is the number
    of open paper positions, which the panel offers to close with the switch (UX-01)."""
    env: Settings = request.app.state.env
    requirements = live_requirements(env, request.app.state.client)
    capital = settings.live_capital_eur
    advice = [
        {
            "ok": capital > 0,
            "label": f"Kapitaal voor de bot: {fmt_eur(capital, 0)}"
            if capital > 0
            else "Kapitaal voor de bot staat op 0: de limieten gelden voor je hele account",
            "fix": "",
            "href": "#f-live_capital_eur",
            "link": "Kapitaal instellen",
        },
        {
            # Without a key the requirement above asks for one (that can trade, not withdraw).
            "skip": key_state["state"] == "missing",
            "ok": key_state["state"] == "ok",
            "label": {
                "ok": "De Bitvavo-sleutel werkt",
                "rejected": "Bitvavo weigert de sleutel: " + str(key_state.get("reason")),
                "no_trading": "De Bitvavo-sleutel mag niet handelen",
                "set": "De Bitvavo-sleutel is nog niet getest",
                "missing": "Nog geen Bitvavo-sleutel om te testen",
            }[key_state["state"]],
            "fix": "Geef hem geen rechten om geld op te nemen; Crypie kan dat niet zien.",
            "href": "#omgeving",
            "link": "Verbinding testen",
        },
        {
            "ok": notify_configured(env),
            "label": "Meldingen buiten de app staan aan"
            if notify_configured(env)
            else "Geen meldingskanaal: bij een storing hoor je niets buiten de app",
            "fix": "",
            "href": "#omgeving",
            "link": "Meldingen bekijken",
        },
    ]
    return {
        "requirements": requirements,
        "missing": [r for r in requirements if not r["ok"]],
        "advice": [a for a in advice if not a.get("skip")],
        "paper_open": paper_open,
        "can_switch": settings.trading_mode != "live" and all(r["ok"] for r in requirements),
        "summary": [
            ("Maximum per order", fmt_eur(settings.max_trade_eur, 0)),
            (
                "Kapitaal voor de bot",
                fmt_eur(capital, 0) if capital > 0 else "0: het hele Bitvavo-account",
            ),
            ("Maximaal in crypto", limit_pct(settings.max_exposure_pct)),
            ("Dagverlieslimiet", limit_pct(settings.daily_loss_limit_pct)),
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


def _form_text(text: object) -> str:
    """A field's text as the page compares it: line ends as \n, no spaces at line ends."""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


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
    known_markets: set[str] | None = None,
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
            # A textarea comes back with \r\n line ends: compare the text, not the bytes.
            f["changed"] = _form_text(sent_form) != _form_text(f["saved_text"])
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
    markets_field = fields_by_key.get("markets")
    if markets_field is not None and known_markets:
        # Saved markets Bitvavo does not list (from MARKETS, or saved while Bitvavo could not
        # be asked): a warning at the field, the save itself checks new ones (UX-53).
        markets_field["warning"] = cfg.unknown_markets(list(settings.markets), known_markets)
    plan = fields_by_key.get("cryptopanic_plan")
    if plan is not None and not env.cryptopanic_token:
        plan["dep_off"] = True
        plan["depends_text"] = "Geldt alleen met een CryptoPanic-token op de server; nu is er geen."
    for group in payload["groups"]:
        group["changed"] = [f for f in group["fields"] if f["overridden"]]
        group["resettable"] = [f for f in group["changed"] if f["key"] != "trading_mode"]
        group["reset_needs_password"] = any(f["sensitive"] for f in group["resettable"])
        group["unsaved"] = [f for f in group["fields"] if f["changed"]]
        # The error summary at the top of the submitted block: a link per field of this block;
        # anything else (a stored value of another block) with its message.
        in_block = {f["key"] for f in group["fields"]}
        group["errors"] = (
            [
                {
                    "key": key,
                    "label": _error_label(key, fields_by_key),
                    "href": "#" + key if key in EXTRA_FORM_FIELDS else "#f-" + key,
                    "message": None if key in in_block or key in EXTRA_FORM_FIELDS else text,
                }
                for key, text in errors.items()
            ]
            if group["key"] == form_group
            else []
        )
        group["reset_confirm"] = _reset_confirm_text(group)
        # A folded block still shows what needs attention inside it: an unknown market or model.
        group["attention"] = [
            f["warning"] if f["key"] == "markets" else f"{f['label']}: onbekend model."
            for f in group["fields"]
            if f.get("warning") and f["key"] not in errors
        ]
    key_state = bitvavo_key_state(request)
    paper_open = open_positions(session, "paper")
    attempts_left = None
    if password_error and password_status == 403 and step_up_enabled(request):
        attempts_left = step_up_attempts_left(request, session)
    return {
        "settings": settings,
        "groups": payload["groups"],
        "paper_field": paper_field,
        # Shown in the Oefenportefeuille block while live: what a reset would close (UX-01).
        "paper_open_markets": [p.market for p in paper_open],
        "live_blocker": payload["live_blocker"],
        "live": _live_context(request, settings, key_state, paper_open=len(paper_open)),
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


@app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
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
        known_markets=await _markets_for_page(request.app),
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


async def _markets_for_page(app_: FastAPI) -> set[str] | None:
    """Bitvavo's market list for a warning on the page (UX-53): markets from MARKETS, or
    saved while Bitvavo could not be asked, are checked too. Not while Bitvavo just failed:
    the page does not wait for it then."""
    if _recent_outage(app_.state.client) is not None:
        cached = getattr(app_.state, "market_names", None)
        return cached[1] if cached else None
    return await _known_markets(app_)


def _saved_text(title: str, changes: list[str]) -> str:
    if not changes:
        return f"Niets gewijzigd in {title}: alles stond al zo."
    return f"{title} opgeslagen; het geldt direct. {_join_changes(changes)}."


@app.post("/settings", response_class=HTMLResponse, include_in_schema=False)
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
    # A part with a form of its own (Weergave, UX-57) saves only its fields; the block saves
    # the rest. ``block`` is where the page shows the result.
    part = str(form.get("part") or "")
    if part and part not in cfg.OWN_FORM_PARTS:
        raise HTTPException(400, "onbekend onderdeel")
    form_keys = cfg.keys_in_form(group, part)
    if not form_keys:
        raise HTTPException(400, "onbekend onderdeel")
    block = part or group
    title = cfg.OWN_FORM_PARTS[part][1] if part else cfg.GROUPS[group][0]
    if action == "reset":
        # The mode is never part of a block reset: it changes only through its own choice.
        keys = [k for k in form_keys if k != "trading_mode"]
        try:
            _, changed = await _reset_settings(request, session, keys, password)
        except StepUpError as exc:
            return step_up_failed(block, exc)
        except SettingsError as exc:
            return page(409, errors=exc.errors, form_group=block)
        text = (
            f"Standaardwaarden hersteld in {title}: {_join_changes(changed)}."
            if changed
            else f"In {title} stond alles al op de standaard."
        )
        return flash.redirect_with_flash(f"/settings?saved={block}#{block}", "ok", text)
    raw: dict[str, str] = {}
    for key in form_keys:
        sent = form.getlist(key)
        if sent:
            raw[key] = str(sent[-1])  # a checkbox posts after its hidden "false" twin
        elif cfg.SPEC_BY_KEY[key].kind == "bool":
            raw[key] = "false"
    extra = {
        "confirm_live": str(form.get("confirm_live") or "")[:20],
        "reset_paper_on_live": str(form.get("reset_paper_on_live") or "") == "1",
    }
    old: Settings = request.app.state.settings
    to_live = False
    try:
        values, candidate = await _check_settings(request, session, raw)
        to_live = candidate.trading_mode == "live" and request.app.state.broker.mode != "live"
        if to_live:
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
            return page(409, submitted=raw, form_group=block, loosened=loosened)
        await _commit_settings(request, session, values, candidate, password)
    except SettingsError as exc:
        return page(400, errors=exc.errors, submitted=raw, form_group=block, form_extra=extra)
    except StepUpError as exc:
        return step_up_failed(block, exc, submitted=raw)
    changes = cfg.changes(old, candidate, form_keys)
    text = _saved_text(title, changes)
    if to_live and extra["reset_paper_on_live"]:
        # Offered in the live panel (UX-01): the paper portfolio starts afresh, so no paper
        # position is guarded next to the real ones. The password was checked for the switch.
        closed = _reset_paper_portfolio(session)
        text += (
            f" De oefenportefeuille is gereset; {closed} open "
            f"{'oefenpositie is' if closed == 1 else 'oefenposities zijn'} gesloten zonder "
            "resultaat."
        )
    # Nothing changed is no success to report: a neutral note instead of a green one.
    kind = "ok" if changes or extra["reset_paper_on_live"] else "info"
    response = flash.redirect_with_flash(f"/settings?saved={block}#{block}", kind, text)
    for key in form_keys:
        warning = cfg.model_warning(getattr(candidate, key)) if key in values else None
        if warning and cfg.SPEC_BY_KEY[key].kind == "model":
            flash.add_flash(
                response,
                "warn",
                f"{cfg.SPEC_BY_KEY[key].label} ({getattr(candidate, key)}): " + warning,
            )
    return response


async def _reset_paper_action(request, session, form, password, page, step_up_failed):
    """Reset the paper portfolio, optionally with a new start amount (saved with it).

    Also while live (UX-01): the exits keep guarding paper positions there, and this is the
    only way to close them. It touches fictive money only and always needs the password
    (require_current_password; live is impossible without login). The start amount cannot
    be changed while live."""
    sent = form.get("paper_start_eur") if request.app.state.broker.mode == "paper" else None
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
        f"Oefenportefeuille gereset. Het saldo staat weer op {fmt_eur(start, 0)}.{positions}",
    )


_last_bitvavo_test = 0.0


@app.post("/settings/bitvavo-test", include_in_schema=False)
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
    fee_text = f" Kosten per order (taker): {fmt_num(fee, 2)}%." if fee is not None else ""
    return flash.redirect_with_flash(
        target,
        "ok",
        f"De sleutel werkt: Bitvavo gaf je accountgegevens.{fee_text} Crypie kan niet zien of "
        "de sleutel mag handelen of geld mag opnemen; controleer bij Bitvavo dat hij alleen "
        "mag lezen en handelen.",
    )


@app.post("/settings/notify-test", include_in_schema=False)
async def settings_notify_test(request: Request):
    """'Testmelding sturen' to the channel set in NOTIFY_URL."""
    ok, text = await send_test(request.app.state.env)
    return flash.redirect_with_flash(
        "/settings?saved=omgeving#omgeving", "ok" if ok else "error", text
    )


# -- Dashboard -------------------------------------------------------------------------

# Bitvavo can be slow or down. The overview waits at most this long for it and then shows
# the last known prices (with their time); the paper balance is local and always shown.
DASHBOARD_TIMEOUT_S = 3.0
# Prices younger than this (from the exit loop, a cycle or another page view) are reused, and
# after a failed fetch the overview waits this long before it asks Bitvavo again.
PRICE_REUSE_S = 15.0
# Per Bitvavo client: when the overview last failed to get prices (monotonic) and why.
_price_failures: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# The order form: the amount filled in for a new buy, and the longest amount text accepted.
ORDER_DEFAULT_EUR = 25.0
AMOUNT_MAX_LENGTH = 32
# What a refused amount may look like when the form shows it again (escaped by the template):
# also '1e3', 'inf' or '-5', so the owner sees what was refused.
_AMOUNT_CHARS = re.compile(r"[0-9A-Za-z .,€+\- ]*")
# "Hele positie verkopen": more than any position is worth. manual_order caps a sell at the
# position, so this sells all of it.
SELL_ALL_EUR = 1e12
# Results of the order form are shown in the order card (where the browser lands, /#order)
# instead of at the top of the page.
ORDER_FLASH_PREFIXES = (
    "Order niet geplaatst",
    "Order verstuurd",
    "Order geplaatst",
    "Gekocht:",
    "Verkocht:",
)
# Set once the owner hides the "Aan de slag" card.
GETTING_STARTED_KEY = "dashboard_getting_started_hidden"


def parse_amount_eur(text: object) -> float:
    """An amount in euro as typed in the order form: '25', '25,50' or '25.50' -> 25.5.

    The same rule as the settings fields (app/numbers.py): a decimal comma is fine (a Dutch
    keyboard), but an amount whose separator could also separate thousands ('1.000', '2,500',
    '1.000,50') is refused rather than guessed, and so is anything that is not a plain positive
    number ('1e3', 'inf', '-5', '25,'). Raises ValueError with a Dutch message.
    """
    cleaned = strip_spaces(str(text or "").replace("€", ""))
    if not cleaned:
        raise ValueError("vul een bedrag in")
    if len(cleaned) > AMOUNT_MAX_LENGTH:
        raise ValueError("het bedrag is te lang")
    invalid = "het bedrag is geen geldig getal; schrijf het als 25 of 25,50"
    try:
        sign, whole, separator, frac = split_decimal(cleaned)
    except AmbiguousNumber:
        raise ValueError(ambiguous_nl(cleaned, "1000 of 25,50")) from None
    except ValueError:
        raise ValueError(invalid) from None
    if sign or not whole or (separator and not frac):
        raise ValueError(invalid)
    if len(frac) > 2:
        raise ValueError("gebruik hoogstens twee cijfers achter de komma")
    return float(f"{whole}.{frac or '0'}")


def _amount_text(value: float) -> str:
    """25.0 -> '25', 25.5 -> '25,50': how the order form shows an amount."""
    return str(int(value)) if value == int(value) else fmt_num(value, 2)


def _default_amount(limits: RiskLimits) -> str:
    """The amount a new buy starts with: € 25, within the limits."""
    return _amount_text(min(max(ORDER_DEFAULT_EUR, limits.min_trade_eur), limits.max_trade_eur))


def _order_form(
    settings: Settings,
    limits: RiskLimits,
    market: object = None,
    side: object = None,
    amount: object = None,
    sell_all: object = None,
) -> dict:
    """The values the order form shows: what was sent (after a refusal) or the defaults.
    Everything comes from the request, so it is checked; the password is never sent back."""
    markets = list(settings.markets)
    chosen = str(market or "").strip().upper()
    if chosen not in markets:
        chosen = markets[0] if markets else ""
    direction = side if side in ("buy", "sell") else "buy"
    if amount is None:
        text = _default_amount(limits) if direction == "buy" else ""
    else:
        text = str(amount)[:AMOUNT_MAX_LENGTH]
        if not _AMOUNT_CHARS.fullmatch(text):
            text = ""
    return {
        "market": chosen,
        "side": direction,
        "amount": text.strip(),
        "sell_all": direction == "sell" and str(sell_all or "") in ("1", "on", "true"),
    }


def _order_problem_nl(text: str) -> str:
    """A refusal as the order card shows it (see app.web.refusal_nl)."""
    return refusal_nl(text)


def _bitvavo_problem(exc: BaseException) -> str:
    """What Bitvavo did, in Dutch: a refused key, a time-out or a failure each have their own
    text, so a refused key is never called 'niet bereikbaar'."""
    if isinstance(exc, BitvavoError) and (exc.status in (401, 403) or exc.code in KEY_PROBLEMS_NL):
        if "No Bitvavo API key" in str(exc):
            return "er is geen Bitvavo-sleutel ingesteld"
        if exc.code in KEY_PROBLEMS_NL:
            # The same reason the footer and Instellingen give for this refusal.
            return (
                f"Bitvavo weigert de API-sleutel (fout {exc.status}): {KEY_PROBLEMS_NL[exc.code]}"
            )
        return (
            f"Bitvavo weigert de API-sleutel (fout {exc.status}); controleer de sleutel, de "
            "rechten 'bekijken' en 'handelen' en de IP-whitelist"
        )
    if isinstance(exc, TimeoutError):
        return f"Bitvavo antwoordde niet binnen {DASHBOARD_TIMEOUT_S:g} seconden"
    if isinstance(exc, BitvavoError) and not exc.ambiguous and not exc.rate_limited:
        reason = exc.payload.get("error") if isinstance(exc.payload, dict) else exc.payload
        return f"Bitvavo weigerde de order (fout {exc.status}: {str(reason)[:160]})"
    return describe_problem(exc)


def _order_done(order: object, market: str, side: str, mode: str, closed: bool) -> tuple:
    """(kind, text) of the message after a placed order: what was bought or sold, for how
    much, at what price and with what fee, in which mode."""
    money = "oefengeld" if mode == "paper" else "echt geld"
    status = str(getattr(order, "status", "") or "")
    if status not in ("filled", "partiallyFilled"):
        return (
            "warn",
            f"Order geplaatst ({money}), status: {fmt_nl(status) or 'onbekend'}. Het resultaat "
            "verschijnt bij Laatste orders zodra Bitvavo het bevestigt.",
        )
    coin = market.split("-")[0]
    coins = getattr(order, "amount", None)
    quote = getattr(order, "amount_quote", None)
    price = getattr(order, "price", None)
    fee = getattr(order, "fee_quote", None)
    head = ("Gekocht: " if side == "buy" else "Verkocht: ") + (
        f"{fmt_amount(coins)} {coin}" if coins else coin
    )
    if quote:
        head += f" voor {fmt_eur(quote)}"
    if price:
        head += f" tegen {fmt_price_eur(price)}"
    parts = [f"{head} ({money})"]
    if fee:
        parts.append(f"kosten {fmt_eur(fee)}")
    if status == "partiallyFilled":
        parts.append("deels uitgevoerd")
    if side == "sell" and closed:
        parts.append("positie gesloten")
    return "ok", " · ".join(parts)


def _portfolio(
    balances: dict[str, float],
    prices_: dict[str, float],
    entry_prices: dict[str, float] | None = None,
) -> dict | None:
    """Total value in EUR of cash plus coins at current prices, or None without data.

    A coin without a price is valued at the entry price of the bot's open position in it
    (``estimated``); a coin without either is left out of the total (``missing``), so the
    page never shows a made-up loss.
    """
    if not balances:
        return None
    entry_prices = entry_prices or {}
    cash = balances.get("EUR", 0.0)
    coins = 0.0
    estimated: list[str] = []
    missing: list[str] = []
    for symbol, amount in balances.items():
        market = f"{symbol}-EUR"
        if symbol == "EUR" or amount <= 0:
            continue
        if market in prices_:
            coins += amount * prices_[market]
        elif market in entry_prices:
            coins += amount * entry_prices[market]
            estimated.append(symbol)
        else:
            missing.append(symbol)
    return {
        "cash_eur": round(cash, 2),
        "coins_eur": round(coins, 2),
        "total_eur": round(cash + coins, 2),
        "estimated": estimated,
        "missing": missing,
    }


def _position_rows(positions: list[Position], prices_: dict[str, float], fee_pct: float) -> dict:
    """Open positions with their result as if sold now: net, so after the fee paid on the buy
    (in entry_quote) and the fee a sell would cost. The same sum as the engine's exit checks,
    and the same function as Handelen (display.position_view)."""
    rows = []
    for p in positions:
        view = display.position_view(p, prices_.get(p.market), fee_pct)
        rows.append(
            {
                "position": p,
                "coin": p.market.split("-")[0],
                "price": view["price"],
                "value_eur": view["value_eur"],
                "pnl_eur": view["pnl_eur"],
                "pnl_pct": view["pnl_pct"],
            }
        )
    known = [r for r in rows if r["pnl_eur"] is not None]
    spent = sum(r["position"].entry_quote for r in known)
    total = sum(r["pnl_eur"] for r in known)
    return {
        "rows": rows,
        "pnl_eur": total if known else None,
        "pnl_pct": total / spent * 100 if known and spent else None,
        "complete": len(known) == len(rows),
    }


async def _dashboard_prices(
    engine: TradingEngine, client: BitvavoClient, markets: list[str]
) -> tuple[dict[str, float], datetime | None, str | None]:
    """(prices, as of, problem). Fresh prices are reused for a few seconds; when Bitvavo does
    not answer in time, the last known prices come back with their time and the reason."""
    now = datetime.now(UTC)
    known = engine.last_prices or {}
    at = engine.last_prices_at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if at and (now - at).total_seconds() <= PRICE_REUSE_S and all(m in known for m in markets):
        return {m: known[m] for m in markets}, at, None
    last_known = {m: known[m] for m in markets if m in known}
    failed = _price_failures.get(client)
    if failed and time.monotonic() - failed[0] <= PRICE_REUSE_S:
        # Bitvavo just failed: do not make every page view wait for it again.
        return last_known, at, failed[1]
    try:
        fresh = await asyncio.wait_for(client.ticker_prices(markets), DASHBOARD_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the page must render whatever Bitvavo does
        log.warning("overview: prices not fetched (%s): %s", type(exc).__name__, exc)
        problem = _bitvavo_problem(exc)
        _price_failures[client] = (time.monotonic(), problem)
        _note_outage(client, exc, problem)  # the charts that load next need not wait again
        return last_known, at, problem
    _price_failures.pop(client, None)
    engine.remember_prices(fresh)
    return fresh, datetime.now(UTC), None


async def _dashboard_balances(
    broker: Broker, session: Session
) -> tuple[dict[str, float], str | None]:
    """(balances, problem). The paper wallet is local; a live balance waits for Bitvavo at
    most DASHBOARD_TIMEOUT_S seconds."""
    try:
        if broker.mode == "paper":
            return await broker.balances(session), None
        return await asyncio.wait_for(broker.balances(session), DASHBOARD_TIMEOUT_S), None
    except Exception as exc:  # noqa: BLE001
        log.warning("overview: balance not fetched (%s): %s", type(exc).__name__, exc)
        return {}, _bitvavo_problem(exc)


def _getting_started(
    request: Request, session: Session, settings: Settings, mode: str
) -> dict | None:
    """The 'Aan de slag' card: shown until there is a trading plan and a first order (or the
    owner hides it), with per step whether it is done."""
    if session.get(Setting, GETTING_STARTED_KEY) is not None:
        return None
    plan_written = not load_plan(session).is_empty()
    traded = (session.scalar(select(func.count(Order.id)).where(Order.mode == mode)) or 0) > 0
    if plan_written and traded:
        return None
    overrides = cfg.load_overrides(session)
    tuned = any(k in overrides for k in (*cfg.keys_in_group("limits"), "markets"))
    blocker = live_blocker(settings, request.app.state.client)
    # The first missing live requirement in plain words, its variable as secondary text.
    missing = next(
        (r for r in live_requirements(settings, request.app.state.client) if not r["ok"]), None
    )
    steps = [
        {
            "title": "Markten en limieten kiezen",
            "text": "Welke munten de bot volgt, en hoeveel hij per order en per dag mag inzetten.",
            "href": "/settings#trading",
            "link": "Naar Instellingen",
            "done": tuned,
        },
        {
            "title": "Strategie bekijken en testen",
            "text": "Lees wanneer de bot koopt en verkoopt, en test de regels op oude koersen.",
            "href": "/strategy",
            "link": "Naar Strategie",
            "done": session.get(Setting, STRATEGY_KEY) is not None,
        },
        {
            "title": "Tradingplan schrijven",
            "text": "Je doel en je eigen regels in gewone taal. Claude krijgt ze mee; de harde "
            "limieten gaan altijd voor.",
            "href": "/plan",
            "link": "Naar Tradingplan",
            "done": plan_written,
        },
        {
            "title": "Claude-sleutel instellen (optioneel)",
            "text": "Zonder sleutel wordt nieuws niet beoordeeld en volgt de bot alleen de regels.",
            "env": "ANTHROPIC_API_KEY",
            "note": "Zet de sleutel in de serveromgeving:",
            "done": bool(settings.anthropic_api_key),
        },
        {
            "title": "Voorwaarden voor live (als je klaar bent met oefenen)",
            "text": "Live handelen kan pas met een login, een Bitvavo-sleutel en een bevestiging "
            "in de serveromgeving.",
            "note": f"Nu nog niet: {missing['missing_nl']}." if missing else "In orde.",
            "env": missing["env"] if missing else None,
            "href": "/settings#trading",
            "link": "Naar Instellingen › Handelen",
            "done": blocker is None,
        },
    ]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"])}


class LiveAdvice(str):
    """One piece of advice for live mode. The string itself is the whole sentence with the
    server variable (for the log and the API); the overview shows ``text`` with a link and the
    variable as secondary text (UX-51)."""

    text: str
    env: str
    href: str
    link: str

    def __new__(cls, text: str, env: str, href: str, link: str) -> LiveAdvice:
        advice = super().__new__(cls, f"{text} ({env})")
        advice.text, advice.env, advice.href, advice.link = text, env, href, link
        return advice


def _live_reminders(env: Settings, settings: Settings) -> list[LiveAdvice]:
    """Advice for live mode that is not enforced (enforcing it would stop live trading on the
    next deploy): own capital for the bot, and a restricted Bitvavo key."""
    out = []
    if settings.live_capital_eur <= 0:
        out.append(
            LiveAdvice(
                "Er is geen kapitaal voor de bot ingesteld, dus de limieten rekenen met je hele "
                "Bitvavo-account, ook met coins die je buiten de bot houdt. Stel in hoeveel de "
                "bot mag beheren, of gebruik een apart subaccount.",
                "LIVE_CAPITAL_EUR",
                "/settings#f-live_capital_eur",
                "Kapitaal instellen",
            )
        )
    if not env.bitvavo_key_restricted:
        out.append(
            LiveAdvice(
                "Geef de Bitvavo-sleutel alleen de rechten 'bekijken' en 'handelen' (nooit "
                "'opnemen') en een IP-whitelist op het adres van je server. Bevestig dat daarna "
                "in de serveromgeving; dan verdwijnt dit advies.",
                "BITVAVO_KEY_RESTRICTED=true",
                "/settings#omgeving",
                "Sleutel bekijken",
            )
        )
    return out


async def _dashboard_context(
    request: Request,
    client: BitvavoClient,
    broker: Broker,
    budget: BudgetGuard,
    session: Session,
    *,
    order_form: dict | None = None,
    order_flashes: list[dict] | None = None,
) -> dict:
    settings: Settings = request.app.state.settings
    engine: TradingEngine = request.app.state.engine
    mode = engine.mode
    positions = open_positions(session, mode)
    # The prices the page needs: the tracked markets, plus a market with an open position
    # that is no longer tracked.
    markets = list(dict.fromkeys([*settings.markets, *(p.market for p in positions)]))
    (prices_, prices_at, price_problem), (balances, balance_problem) = await asyncio.gather(
        _dashboard_prices(engine, client, markets), _dashboard_balances(broker, session)
    )
    strategy, strategy_warning = load_strategy_checked(session)
    paused = risk.is_paused(session)
    figures = risk_now(
        engine,
        session,
        settings,
        balances=balances or None,
        prices=None if price_problem else prices_ or None,
    )
    portfolio = _portfolio(balances, prices_, {p.market: p.entry_price for p in positions})
    last = engine.last_portfolio
    portfolio_before = (
        {"total_eur": round(last["total"], 2), "at": last.get("at")}
        if portfolio is None and last and last.get("mode") == mode and last.get("total")
        else None
    )
    limits = engine.limits
    positions_view = _position_rows(positions, prices_, engine.fee_pct)
    return {
        "settings": settings,
        "mode": mode,
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
        "prices_at": prices_at,
        "price_problem": price_problem,
        # Bitvavo answered but had no price for these (a market it does not list, UX-53).
        "unpriced_markets": []
        if price_problem
        else [m for m in settings.markets if not prices_.get(m)],
        "balances": balances,
        "balance_problem": balance_problem,
        "portfolio": portfolio,
        "portfolio_before": portfolio_before,
        "orders": recent_orders(session, 20, mode=mode),
        "rate_limit": client.rate_limit_remaining,
        "budget": budget.status(session),
        "strategy": strategy,
        # A proposal of Claude that waits on Handelen (same rule as the strategy page).
        "waiting_proposal": _waiting_proposal(session, settings, strategy),
        "strategy_warning": strategy_warning,
        "sentiments": [market_sentiment(session, c, 48) for c in settings.coins],
        "open_positions": positions,
        "positions_view": positions_view,
        # Live mode: paper positions that the exit loop still guards (with fictitious money).
        "paper_positions": open_positions(session, "paper") if mode == "live" else [],
        "performance": performance_summary(session, mode),
        "paused": paused,
        "config_warning": request.app.state.config_warning,
        "unprotected_positions": engine.unprotected,
        "session_secret_weak": getattr(request.app.state, "session_secret_weak", False),
        "live_reminders": (
            _live_reminders(request.app.state.env, settings) if mode == "live" else []
        ),
        "getting_started": _getting_started(request, session, settings, mode),
        "chart_periods": list(PERIODS),
        "fee_pct": engine.fee_pct,
        "order_form": order_form or _order_form(settings, limits),
        "order_default_amount": _default_amount(limits),
        "order_token": secrets.token_urlsafe(16),
        "order_flashes": order_flashes or [],
        # A refused password: the field is marked, so app.js puts the focus there.
        "order_password_error": any(
            f.get("kind") == "error" and "wachtwoord" in str(f.get("text", "")).lower()
            for f in order_flashes or []
        ),
        "order_limits": {
            "min_buy": max(limits.min_trade_eur, EXCHANGE_MIN_ORDER_EUR),
            "max_buy": limits.max_trade_eur,
            "min_sell": EXCHANGE_MIN_ORDER_EUR,
        },
        # Per market the open position, for the sell side of the order form.
        "order_positions": {
            r["position"].market: {
                "coin": r["coin"],
                "amount": r["position"].amount,
                "value_eur": round(r["value_eur"], 2) if r["value_eur"] is not None else None,
            }
            for r in positions_view["rows"]
        },
        "order_needs_password": mode == "live" and step_up_enabled(request),
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    budget: BudgetGuard = Depends(get_budget),
    session: Session = Depends(get_session),
):
    # After a refused order the form comes back with what was sent (?market=…&side=…&amount=…).
    q = request.query_params
    engine: TradingEngine = request.app.state.engine
    form = _order_form(
        request.app.state.settings,
        engine.limits,
        q.get("market"),
        q.get("side"),
        q.get("amount"),
        q.get("sell_all"),
    )
    flashes = list(getattr(request.state, "flashes", None) or [])
    mine = [f for f in flashes if str(f.get("text", "")).startswith(ORDER_FLASH_PREFIXES)]
    if mine:
        request.state.flashes = [f for f in flashes if f not in mine]
    ctx = await _dashboard_context(
        request, client, broker, budget, session, order_form=form, order_flashes=mine
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/api/dashboard/status")
async def dashboard_status(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """The hero's status block, rendered by the same template as the page, for the overview
    to refresh itself while it stays open. No Bitvavo calls."""
    paused = risk.is_paused(session)
    health = _health(request, engine, session, paused=paused)
    html = templates.get_template("_dashboard_status.html").render(
        health=health, paused=paused, settings=request.app.state.settings, mode=engine.mode
    )
    body = {"html": html, "level": health["level"], "paused": paused}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.post("/dashboard/getting-started", include_in_schema=False)
async def hide_getting_started(session: Session = Depends(get_session)):
    """Hide the 'Aan de slag' card for good."""
    row = session.get(Setting, GETTING_STARTED_KEY)
    if row is None:
        session.add(Setting(key=GETTING_STARTED_KEY, value="1"))
        session.commit()
    return flash.redirect_with_flash(
        "/",
        "info",
        "De kaart 'Aan de slag' is verborgen. De stappen vind je terug onder Instellingen, "
        "Strategie en Tradingplan.",
    )


# One use per rendered order form (UX-100). app.js stops a double click; this stops the same
# form being sent again later (Back and send again, a browser retrying a slow request), which
# would otherwise place a second order. Kept in memory: one process (uvicorn --workers 1).
ORDER_TOKEN_TTL_S = 30 * 60
_order_tokens: dict[str, float] = {}


def _claim_order_token(token: str) -> bool:
    """True the first time a form's token places an order (within the TTL), False after."""
    now = time.monotonic()
    for old, at in list(_order_tokens.items()):
        if now - at > ORDER_TOKEN_TTL_S:
            del _order_tokens[old]
    if token in _order_tokens:
        return False
    _order_tokens[token] = now
    return True


def _release_order_token(token: str, claimed: bool) -> None:
    """Forget a token claimed by this request when its order was refused before anything was
    sent: no order exists, so sending the form again is not a repeat."""
    if claimed and token:
        _order_tokens.pop(token, None)


class _OrderResent(Exception):
    """The order form was sent before: nothing is placed again."""


ORDER_RESENT = (
    "Order niet geplaatst: dit formulier was al verstuurd, dus de order is niet nog een keer "
    "geplaatst. Kijk bij Laatste orders wat er gebeurde."
)


@app.post("/orders", include_in_schema=False)
async def dashboard_order(
    request: Request,
    market: str = Form(..., max_length=32),
    side: str = Form(..., max_length=8),
    amount_quote: str = Form("", max_length=64),
    sell_all: str = Form("", max_length=8),
    current_password: str = Form(""),
    order_token: str = Form("", max_length=64),
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    budget: BudgetGuard = Depends(get_budget),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    """A manual order from the overview. The result is shown in the order card: a placed
    order via a one-time message after a redirect to /#order; a refusal the same way (with
    the form filled in as it was sent) for a browser, or on the page itself for other clients."""
    settings: Settings = request.app.state.settings
    form = _order_form(settings, engine.limits, market, side, amount_quote, sell_all)
    mode = engine.mode
    kind, status = "error", 400
    claimed = False
    try:
        amount = SELL_ALL_EUR if form["sell_all"] else parse_amount_eur(amount_quote)
        if mode == "live":
            # Real money: ask for the current password again (step-up).
            await require_current_password(request, session, current_password)
        if order_token and not _claim_order_token(order_token):
            kind, status = "warn", 409
            raise _OrderResent
        claimed = bool(order_token)
        order = await engine.manual_order(session, market, side, amount)
    except _OrderResent:
        text = ORDER_RESENT
    except StepUpError as exc:
        # A full sentence of its own ('Het wachtwoord klopt niet.'), so no colon before it.
        text, status = f"Order niet geplaatst. {exc.message}", exc.status
    except OrderUncertain:
        kind, status = "warn", 202
        text = (
            "Order verstuurd, maar de uitkomst is nog niet bekend: Bitvavo bevestigde hem niet op "
            "tijd. De bot controleert het vanzelf; kijk zo bij Laatste orders en plaats hem niet "
            "opnieuw."
        )
    except BitvavoError as exc:
        if isinstance(exc, ValueError):  # an invalid market name: nothing was sent
            text = f"Order niet geplaatst: {_order_problem_nl(str(exc))}."
            _release_order_token(order_token, claimed)
        else:
            text, status = f"Order niet geplaatst: {_bitvavo_problem(exc)}.", 502
    except ValueError as exc:
        # Refused by Crypie's own checks before anything reached Bitvavo: the same form may
        # be sent again after a fix (the token only guards against placing an order twice).
        text = f"Order niet geplaatst: {_order_problem_nl(str(exc))}."
        _release_order_token(order_token, claimed)
    else:
        closed = side == "sell" and open_position(session, mode, form["market"]) is None
        kind, text = _order_done(order, form["market"], side, mode, closed)
        return flash.redirect_with_flash("/#order", kind, text)
    if errors.wants_html(request):
        query = {"market": form["market"], "side": form["side"], "amount": form["amount"]}
        if form["sell_all"]:
            query["sell_all"] = "1"
        return flash.redirect_with_flash(f"/?{urlencode(query)}#order", kind, text)
    ctx = await _dashboard_context(
        request,
        client,
        broker,
        budget,
        session,
        order_form=form,
        order_flashes=[{"kind": kind, "text": text}],
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx, status_code=status)
