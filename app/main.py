"""FastAPI entrypoint: JSON API plus a small dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.bitvavo.broker import Broker, LiveBroker, PaperBroker, recent_orders
from app.bitvavo.client import BitvavoClient, BitvavoError
from app.config import Settings, get_settings
from app.db import get_session, init_db

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_broker(settings: Settings, client: BitvavoClient) -> Broker:
    if settings.trading_mode == "live":
        if not client.authenticated:
            raise RuntimeError("TRADING_MODE=live requires BITVAVO_API_KEY and BITVAVO_API_SECRET")
        return LiveBroker(client)
    return PaperBroker(client, settings.paper_start_eur)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    client = BitvavoClient(
        settings.bitvavo_api_key, settings.bitvavo_api_secret, settings.bitvavo_rest_url
    )
    app.state.settings = settings
    app.state.client = client
    app.state.broker = build_broker(settings, client)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="Crypie", version="0.1.0", lifespan=lifespan)


def get_client(request: Request) -> BitvavoClient:
    return request.app.state.client


def get_broker(request: Request) -> Broker:
    return request.app.state.broker


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
    return {"id": o.id, "status": o.status, "price": o.price, "amount": o.amount, "mode": o.mode}


# -- Dashboard -------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    client: BitvavoClient = Depends(get_client),
    broker: Broker = Depends(get_broker),
    session: Session = Depends(get_session),
):
    settings: Settings = request.app.state.settings
    error = None
    prices_: dict[str, float] = {}
    balances: dict[str, float] = {}
    try:
        prices_ = await client.ticker_prices(settings.markets)
        balances = await broker.balances(session)
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "settings": settings,
            "prices": prices_,
            "balances": balances,
            "orders": recent_orders(session, 20),
            "error": error,
            "rate_limit": client.rate_limit_remaining,
        },
    )


@app.post("/orders")
async def dashboard_order(
    request: Request,
    market: str = Form(...),
    side: str = Form(...),
    amount_quote: float = Form(...),
    broker: Broker = Depends(get_broker),
    session: Session = Depends(get_session),
):
    try:
        await broker.market_order(session, market.upper(), side, amount_quote, reason="manual")
    except (ValueError, BitvavoError) as exc:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": request.app.state.settings,
                "prices": {},
                "balances": {},
                "orders": recent_orders(session, 20),
                "error": str(exc),
                "rate_limit": None,
            },
            status_code=400,
        )
    return RedirectResponse("/", status_code=303)
