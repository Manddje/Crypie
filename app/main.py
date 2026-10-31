"""FastAPI entrypoint: JSON API plus a small dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store as cfg
from app.ai.budget import BudgetExceeded, BudgetGuard
from app.ai.decision_maker import DecisionMaker
from app.ai.evaluator import Evaluator
from app.ai.news_analyst import NewsAnalyst
from app.ai.strategy_assistant import StrategyAssistant
from app.auth.routes import require_login
from app.auth.routes import router as auth_router
from app.auth.service import AuthService, load_or_create_session_secret
from app.bitvavo.broker import PAPER_WALLET_KEY, Broker, LiveBroker, PaperBroker, recent_orders
from app.bitvavo.client import BitvavoClient, BitvavoError
from app.config import Settings, get_settings
from app.db import Evaluation, SessionLocal, Setting, get_session, init_db
from app.news.service import NewsService, market_sentiment, recent_items
from app.news.sources import NewsFetcher
from app.settings_store import SettingsError
from app.strategy.engine import backtest, latest_snapshot
from app.strategy.model import Strategy
from app.strategy.store import load_strategy, save_strategy
from app.trading import risk
from app.trading.engine import (
    TradingEngine,
    closed_positions,
    latest_decisions,
    open_positions,
    performance_summary,
    recent_decisions,
)
from app.trading.risk import RiskLimits
from app.web import STATIC_DIR, templates

log = logging.getLogger("crypie")
# Uvicorn only configures its own loggers; without this the cycle and news results never show
# up in the container logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def live_blocker(settings: Settings, client: BitvavoClient) -> str | None:
    """Why live trading is not allowed right now, or None when it is.

    Both conditions live in the environment on purpose: switching to live in the portal is
    never enough on its own.
    """
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


def build_auth(settings: Settings) -> AuthService | None:
    """Login is on unless AUTH_ENABLED=false; the account itself is created at /setup."""
    if not settings.auth_enabled:
        log.warning("AUTH_ENABLED=false: the dashboard is reachable without logging in")
        return None
    secret = settings.session_secret
    if not secret:
        with SessionLocal() as session:
            secret = load_or_create_session_secret(session)
    return AuthService(settings, secret)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = get_settings()
    init_db()
    app.state.env = env
    app.state.config_warning = None
    # Login settings come from the environment only; they are not editable in the portal.
    app.state.auth = build_auth(env)
    with SessionLocal() as session:
        settings = cfg.effective_settings(session, env)
    client = BitvavoClient(env.bitvavo_api_key, env.bitvavo_api_secret, env.bitvavo_rest_url)
    app.state.client = client
    if client.authenticated:
        # Use the account's real taker fee when Bitvavo tells us; otherwise the configured one.
        real_fee = await client.taker_fee_pct()
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
    )
    # The schedulers read the interval and on/off switch from app.state.settings on every
    # tick, so a change in the portal takes effect within a tick, without a restart.
    tasks = [
        asyncio.create_task(news_scheduler(app)),
        asyncio.create_task(trading_scheduler(app)),
        asyncio.create_task(evaluation_scheduler(app)),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await fetcher.aclose()
        await client.aclose()


SCHEDULER_TICK_S = 30


async def _run_periodically(
    app: FastAPI,
    name: str,
    job,
    seconds_between,
    initial_delay: float,
    run_at_start: bool = True,
) -> None:
    """Run ``job(session)`` whenever ``seconds_between(settings)`` has passed since the last run.

    ``seconds_between`` returns 0 or less to switch the job off; it is re-read every tick.
    With ``run_at_start`` False the first run waits one full interval.
    """
    await asyncio.sleep(initial_delay)
    loop = asyncio.get_running_loop()
    last: float | None = None if run_at_start else loop.time()
    while True:
        interval = seconds_between(app.state.settings)
        now = loop.time()
        if interval > 0 and (last is None or now - last >= interval):
            last = now
            try:
                with SessionLocal() as session:
                    await job(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed", name)
        await asyncio.sleep(SCHEDULER_TICK_S)


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


app = FastAPI(title="Crypie", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.middleware("http")(require_login)
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
async def health(request: Request):
    return {"status": "ok", "mode": request.app.state.settings.trading_mode}


@app.get("/api/prices")
async def prices(request: Request, client: BitvavoClient = Depends(get_client)):
    try:
        return await client.ticker_prices(request.app.state.settings.markets)
    except (BitvavoError, Exception) as exc:
        raise HTTPException(502, f"Bitvavo unreachable: {exc}") from exc


@app.get("/api/candles/{market}")
async def candles(
    market: str, interval: str = "1h", limit: int = 100, client: BitvavoClient = Depends(get_client)
):
    try:
        return await client.candles(market.upper(), interval, limit)
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/balance")
async def balance(broker: Broker = Depends(get_broker), session: Session = Depends(get_session)):
    try:
        return {"mode": broker.mode, "balances": await broker.balances(session)}
    except BitvavoError as exc:
        raise HTTPException(502, str(exc)) from exc


class OrderIn(BaseModel):
    market: str
    side: str = Field(pattern="^(buy|sell)$")
    amount_quote: float = Field(gt=0, description="Amount in EUR")
    reason: str | None = None


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
    broker: Broker = Depends(get_broker),
    session: Session = Depends(get_session),
):
    market = order.market.upper()
    if market not in request.app.state.settings.markets:
        raise HTTPException(400, f"{market} is not in MARKETS")
    try:
        o = await broker.market_order(session, market, order.side, order.amount_quote, order.reason)
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
    description: str = Field(min_length=5, max_length=4000)


class BacktestIn(BaseModel):
    strategy: Strategy | None = None
    limit: int = Field(default=500, ge=50, le=1440)
    start_eur: float = Field(default=1000, gt=0)


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


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(
    request: Request,
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    strategy = load_strategy(session)
    return templates.TemplateResponse(
        request,
        "strategy.html",
        {
            "settings": request.app.state.settings,
            "strategy_json": strategy.model_dump_json(indent=2),
            "strategy": strategy,
            "assistant_available": assistant.available,
            "proposal": None,
            "explanation": None,
            "backtest": None,
            "error": None,
            "message": None,
        },
    )


@app.post("/strategy", response_class=HTMLResponse)
async def strategy_action(
    request: Request,
    action: str = Form(...),
    strategy_json: str = Form(""),
    description: str = Form(""),
    limit: int = Form(500),
    client: BitvavoClient = Depends(get_client),
    assistant: StrategyAssistant = Depends(get_assistant),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    ctx: dict = {
        "settings": settings,
        "strategy_json": strategy_json,
        "strategy": load_strategy(session),
        "assistant_available": assistant.available,
        "proposal": None,
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
            proposal = await assistant.propose(
                session, description, ctx["strategy"], settings.markets
            )
            ctx["strategy_json"] = proposal.strategy.model_dump_json(indent=2)
            ctx["proposal"] = proposal.strategy
            ctx["explanation"] = proposal.explanation
            ctx["description"] = description
            ctx["backtest"] = await _run_backtest(request, client, proposal.strategy, limit, 1000)
            ctx["message"] = (
                "Claude heeft een voorstel gemaakt. "
                "Bekijk het hieronder en sla het op als je het goed vindt."
            )
        else:
            candidate = Strategy.model_validate_json(strategy_json)
            if candidate.market not in settings.markets:
                raise ValueError(f"{candidate.market} staat niet in MARKETS")
            if action == "backtest":
                ctx["proposal"] = candidate
                ctx["backtest"] = await _run_backtest(request, client, candidate, limit, 1000)
            elif action == "save":
                save_strategy(session, candidate)
                ctx["strategy"] = candidate
                ctx["message"] = f"Strategie '{candidate.name}' opgeslagen en actief."
            else:
                raise ValueError(f"Onbekende actie {action!r}")
    except ValidationError as exc:
        ctx["error"] = "Ongeldige strategie: " + "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        status = 400
    except (ValueError, RuntimeError, BitvavoError) as exc:
        ctx["error"] = str(exc)
        status = 400
    return templates.TemplateResponse(request, "strategy.html", ctx, status_code=status)


# -- News -----------------------------------------------------------------------------


def _news_dict(it) -> dict:
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
    }


@app.get("/api/news")
async def list_news(hours: int = 24, limit: int = 100, session: Session = Depends(get_session)):
    return [_news_dict(it) for it in recent_items(session, hours, min(limit, 500))]


@app.post("/api/news/refresh")
async def refresh_news(
    news: NewsService = Depends(get_news), session: Session = Depends(get_session)
):
    return await news.refresh(session)


@app.get("/api/news/sentiment")
async def news_sentiment(
    request: Request, hours: int = 48, session: Session = Depends(get_session)
):
    return {
        coin: market_sentiment(session, coin, hours).as_dict()
        for coin in request.app.state.settings.coins
    }


@app.get("/news", response_class=HTMLResponse)
async def news_page(
    request: Request,
    hours: int = 24,
    news: NewsService = Depends(get_news),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "settings": settings,
            "hours": hours,
            "items": recent_items(session, hours, 150),
            "sentiments": [market_sentiment(session, c, 48) for c in settings.coins],
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
        "claude_available": engine.decision_maker.available,
        "last_run": engine.last_run,
        "last_results": engine.last_results,
        "last_error": engine.last_error,
        "trades_today": risk.trades_today(session, engine.mode),
        "realized_pnl_today": round(risk.realized_pnl_today(session, engine.mode), 2),
        "performance_30d": performance_summary(session, engine.mode),
    }


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
    limit: int = 50,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    return [_decision_dict(d) for d in recent_decisions(session, engine.mode, min(limit, 500))]


@app.post("/api/evaluations", status_code=201)
async def run_evaluation(
    days: int = 7,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    if not engine.evaluator.available:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured")
    try:
        ev = await engine.run_evaluation(session, days)
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


@app.post("/api/evaluations/{evaluation_id}/apply")
async def apply_evaluation(
    evaluation_id: int, request: Request, session: Session = Depends(get_session)
):
    ev = session.get(Evaluation, evaluation_id)
    if ev is None or not ev.proposed_strategy:
        raise HTTPException(404, "evaluation or proposal not found")
    strategy = Strategy.model_validate_json(ev.proposed_strategy)
    if strategy.market not in request.app.state.settings.markets:
        raise HTTPException(400, f"{strategy.market} is not in MARKETS")
    save_strategy(session, strategy)
    ev.applied = True
    session.commit()
    return {"applied": True, "strategy": strategy.model_dump()}


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(
    request: Request,
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    evaluations = session.scalars(select(Evaluation).order_by(Evaluation.id.desc()).limit(5)).all()
    return templates.TemplateResponse(
        request,
        "trading.html",
        {
            "settings": settings,
            "status": _trading_status(engine, session, settings),
            "open_positions": open_positions(session, engine.mode),
            "closed_positions": closed_positions(session, engine.mode)[:20],
            "decisions": recent_decisions(
                session, engine.mode, limit=5000, days=settings.decision_history_days
            ),
            "last_cycle": latest_decisions(session, engine.mode, list(engine.markets)),
            "evaluations": [_evaluation_dict(e) for e in evaluations],
            "error": None,
        },
    )


@app.post("/trading/action")
async def trading_action(
    action: str = Form(...),
    engine: TradingEngine = Depends(get_engine),
    session: Session = Depends(get_session),
):
    if action == "pause":
        risk.set_paused(session, True)
    elif action == "resume":
        risk.set_paused(session, False)
    elif action == "cycle":
        await engine.run_cycle(session)
    elif action == "evaluate" and engine.evaluator.available:
        await engine.run_evaluation(session)
    elif action.startswith("apply:"):
        ev = session.get(Evaluation, int(action.split(":", 1)[1]))
        if ev is not None and ev.proposed_strategy:
            save_strategy(session, Strategy.model_validate_json(ev.proposed_strategy))
            ev.applied = True
            session.commit()
    return RedirectResponse("/trading", status_code=303)


# -- Settings -------------------------------------------------------------------------


def _editable_values(settings: Settings) -> dict:
    return {key: getattr(settings, key) for key in cfg.EDITABLE_KEYS}


def _settings_payload(request: Request, session: Session) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "mode": settings.trading_mode,
        "live_blocker": live_blocker(request.app.state.env, request.app.state.client),
        "warning": request.app.state.config_warning,
        "values": _editable_values(settings),
        "groups": cfg.describe(session, settings, request.app.state.env),
    }


def _save_settings(request: Request, session: Session, raw: dict) -> Settings:
    """Parse, validate, store and apply; raises SettingsError with per-field messages."""
    app_ = request.app
    values = cfg.parse_values(raw)
    candidate = cfg.preview(session, values, app_.state.env)
    if candidate.trading_mode == "live" and app_.state.broker.mode != "live":
        blocker = live_blocker(app_.state.env, app_.state.client)
        if blocker:
            raise SettingsError({"trading_mode": f"live handelen kan nog niet: {blocker}"})
    cfg.save_overrides(session, values, app_.state.env)
    apply_settings(app_, candidate)
    app_.state.config_warning = None
    return candidate


def _reset_settings(request: Request, session: Session, keys: list[str] | None) -> int:
    removed = cfg.reset_overrides(session, keys)
    new = cfg.effective_settings(session, request.app.state.env)
    if new.trading_mode == "live" and live_blocker(request.app.state.env, request.app.state.client):
        # The environment itself says live but live is not possible: stay in paper mode.
        new = new.model_copy(update={"trading_mode": "paper"})
    apply_settings(request.app, new)
    return removed


@app.get("/api/settings")
async def api_settings(request: Request, session: Session = Depends(get_session)):
    return _settings_payload(request, session)


@app.put("/api/settings")
async def api_update_settings(
    body: dict, request: Request, session: Session = Depends(get_session)
):
    try:
        _save_settings(request, session, body)
    except SettingsError as exc:
        raise HTTPException(422, exc.errors) from exc
    return _settings_payload(request, session)


@app.delete("/api/settings")
async def api_reset_settings(request: Request, session: Session = Depends(get_session)):
    """Forget every value saved in the portal; the environment applies again."""
    _reset_settings(request, session, None)
    return _settings_payload(request, session)


@app.delete("/api/settings/{key}")
async def api_reset_setting(key: str, request: Request, session: Session = Depends(get_session)):
    if key not in cfg.SPEC_BY_KEY:
        raise HTTPException(404, f"{key} is geen aanpasbare instelling")
    _reset_settings(request, session, [key])
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


def _settings_context(
    request: Request,
    session: Session,
    errors: dict[str, str] | None = None,
    submitted: dict | None = None,
    message: str | None = None,
) -> dict:
    payload = _settings_payload(request, session)
    if submitted:
        for group in payload["groups"]:
            for f in group["fields"]:
                if f["key"] in submitted:
                    f["text"] = submitted[f["key"]]
                    if f["kind"] == "bool":
                        f["value"] = str(submitted[f["key"]]).lower() in ("true", "1", "on")
                    elif f["kind"] in ("choice", "model"):
                        f["value"] = submitted[f["key"]]
    env: Settings = request.app.state.env
    return {
        "settings": request.app.state.settings,
        "groups": payload["groups"],
        "live_blocker": payload["live_blocker"],
        "config_warning": payload["warning"],
        "errors": errors or {},
        "message": message,
        "model_choices": [m for m, _ in cfg.MODEL_CHOICES],
        "environment": {
            "bitvavo_key": env.has_bitvavo_credentials,
            "anthropic_key": bool(env.anthropic_api_key),
            "cryptopanic_token": bool(env.cryptopanic_token),
            "live_ack": env.live_trading_ack,
            "database": env.database_url.split(":", 1)[0],
        },
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, saved: str | None = None, session: Session = Depends(get_session)
):
    message = None
    if saved in cfg.GROUPS:
        message = f"Instellingen voor '{cfg.GROUPS[saved][0]}' opgeslagen. Ze gelden direct."
    elif saved == "reset":
        message = "Standaardwaarden hersteld; de waarden uit de serveromgeving gelden weer."
    elif saved == "paper":
        message = (
            "Oefenportefeuille gereset. Het saldo staat weer op het startbedrag en open "
            "oefenposities zijn gesloten."
        )
    ctx = _settings_context(request, session, message=message)
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    group = str(form.get("group") or "")
    action = str(form.get("action") or "save")
    if action == "reset_paper":
        if request.app.state.broker.mode != "paper":
            raise HTTPException(400, "alleen mogelijk in oefenmodus")
        _reset_paper_portfolio(session)
        return RedirectResponse("/settings?saved=paper", status_code=303)
    if group not in cfg.GROUPS:
        raise HTTPException(400, "onbekende groep")
    if action == "reset":
        _reset_settings(request, session, cfg.keys_in_group(group))
        return RedirectResponse(f"/settings?saved=reset#{group}", status_code=303)
    raw: dict[str, str] = {}
    for key in cfg.keys_in_group(group):
        sent = form.getlist(key)
        if sent:
            raw[key] = str(sent[-1])  # a checkbox posts after its hidden "false" twin
        elif cfg.SPEC_BY_KEY[key].kind == "bool":
            raw[key] = "false"
    try:
        _save_settings(request, session, raw)
    except SettingsError as exc:
        ctx = _settings_context(request, session, errors=exc.errors, submitted=raw)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)
    return RedirectResponse(f"/settings?saved={group}#{group}", status_code=303)


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
    mode = request.app.state.engine.mode
    return {
        "settings": settings,
        "prices": prices_,
        "balances": balances,
        "portfolio": _portfolio(balances, prices_),
        "orders": recent_orders(session, 20),
        "error": error,
        "rate_limit": client.rate_limit_remaining,
        "budget": budget.status(session),
        "strategy": load_strategy(session),
        "sentiments": [market_sentiment(session, c, 48) for c in settings.coins],
        "open_positions": open_positions(session, mode),
        "performance": performance_summary(session, mode),
        "paused": risk.is_paused(session),
        "last_run": request.app.state.engine.last_run,
        "config_warning": request.app.state.config_warning,
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
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    budget: BudgetGuard = Depends(get_budget),
    session: Session = Depends(get_session),
):
    try:
        await broker.market_order(session, market.upper(), side, amount_quote, reason="manual")
    except (ValueError, BitvavoError) as exc:
        ctx = await _dashboard_context(
            request, client, broker, budget, session, error=f"Order niet geplaatst: {exc}"
        )
        return templates.TemplateResponse(request, "dashboard.html", ctx, status_code=400)
    return RedirectResponse("/", status_code=303)
