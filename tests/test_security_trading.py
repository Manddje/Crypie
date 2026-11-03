"""Security-audit fixes in the trading core: live order handling, exits, locks, limits.

Live paths run against ``FakeExchange``, a stand-in for the Bitvavo client; the client itself
is tested with respx. Tests that need the real database dialect (column widening, truncation,
rollback per market) use the configured DATABASE_URL, so they also run on Postgres.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.ai.decision_maker import TradeDecision
from app.bitvavo.broker import LiveBroker, OrderUncertain, PaperBroker, floor_amount
from app.bitvavo.client import (
    BitvavoClient,
    BitvavoError,
    BitvavoNetworkError,
    InvalidMarketError,
)
from app.db import Base, Decision, NewsItem, Order, Position, Setting, make_engine, widen_columns
from app.trading import risk
from app.trading.engine import (
    ManualOrderError,
    TradingEngine,
    live_candle_limit,
    news_sources,
    open_position,
)
from tests.test_trading import (
    BUY_SERIES,
    FLAT_SERIES,
    LIMITS,
    SELL_SERIES,
    FakeDecider,
    FakeEvaluator,
    candles,
    cross_strategy,
    make_engine_,
)

BASE = "https://api.bitvavo.com/v2"


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


# -- a fake Bitvavo for the live paths ----------------------------------------------------


def not_found():
    return BitvavoError(404, {"errorCode": 240, "error": "No order found."})


def timeout():
    return BitvavoNetworkError(httpx.ReadTimeout("read timed out"))


class FakeExchange:
    """Prices, candles, a balance and an order book that fills market orders.

    ``script`` holds what the next ``place_order`` calls do: a response dict, an exception,
    ``("lost", response)`` (Bitvavo executes it but the answer never arrives) or None for a
    full fill at the ticker price with a 0.25% EUR fee.
    """

    authenticated = True

    def __init__(self, prices, series=None, balance=None, decimals=8):
        self.prices = prices
        self.series = series or {}
        self.bal = dict(balance or {"EUR": 10_000.0})
        self.decimals = decimals
        self.script: list = []
        self.placed: list[dict] = []
        self.orders: dict[str, dict] = {}  # clientOrderId -> exchange state
        self.cancelled: list[str] = []

    async def ticker_prices(self, markets):
        return {m: self.prices[m] for m in markets if m in self.prices}

    async def ticker_price(self, market):
        return self.prices[market]

    async def candles(self, market, interval, limit):
        return self.series[market]

    async def balance(self):
        return [{"symbol": k, "available": str(v), "inOrder": "0"} for k, v in self.bal.items()]

    async def market_info(self, market):
        return {"market": market, "quantityDecimals": self.decimals}

    def fill(self, market, side, kw, fraction=1.0, status="filled"):
        base, _ = market.split("-")
        price = self.prices[market]
        if side == "buy":
            quote = float(kw["amount_quote"]) * fraction
            fee = quote * 0.0025
            coins = (quote - fee) / price
            self.bal["EUR"] -= quote
            self.bal[base] = self.bal.get(base, 0.0) + coins
            traded = quote - fee
        else:
            coins = float(kw["amount"]) * fraction
            traded = coins * price
            fee = traded * 0.0025
            self.bal[base] = self.bal.get(base, 0.0) - coins
            self.bal["EUR"] += traded - fee
        return {
            "orderId": f"ex-{len(self.placed)}",
            "status": status,
            "filledAmount": f"{coins:.8f}",
            "filledAmountQuote": f"{traded:.8f}",
            "feePaid": f"{fee:.8f}",
            "feeCurrency": "EUR",
        }

    async def place_order(self, market, side, order_type="market", **kw):
        assert order_type == "market"
        self.placed.append({"market": market, "side": side, **kw})
        step = self.script.pop(0) if self.script else None
        if callable(step):
            step = step(market, side, kw)
        if isinstance(step, Exception):
            raise step
        lost = isinstance(step, tuple) and step[0] == "lost"
        resp = step[1] if lost else step
        if resp is None:
            resp = self.fill(market, side, kw)
        self.orders[kw["client_order_id"]] = resp
        if lost:
            raise timeout()
        return resp

    async def get_order(self, market, order_id=None, client_order_id=None):
        for coid, resp in self.orders.items():
            if coid == client_order_id or (order_id and resp.get("orderId") == order_id):
                return resp
        raise not_found()

    async def cancel_order(self, market, order_id):
        self.cancelled.append(order_id)
        for resp in self.orders.values():
            if resp.get("orderId") == order_id and resp["status"] in ("new", "partiallyFilled"):
                resp["status"] = "canceled"
        return {"orderId": order_id}


def live_broker(exchange):
    broker = LiveBroker(exchange)
    broker.poll_delay_s = 0
    return broker


def live_engine(session, exchange, decider=None, strategy=None, markets=None):
    from app.strategy.store import save_strategy

    save_strategy(session, strategy or cross_strategy())
    return TradingEngine(
        exchange,
        live_broker(exchange),
        decider or FakeDecider(),
        FakeEvaluator(None),
        markets or list(exchange.series) or ["BTC-EUR"],
        LIMITS,
        fee_pct=0.25,
    )


def add_position(session, mode="paper", market="BTC-EUR", amount=5.0, entry=10.0, stop=9.0, **kw):
    pos = Position(
        mode=mode,
        market=market,
        amount=amount,
        entry_price=entry,
        entry_quote=amount * entry,
        stop_price=stop,
        high_price=entry,
        **kw,
    )
    session.add(pos)
    session.commit()
    return pos


def buy_decider(amount=100):
    return FakeDecider(
        TradeDecision(action="buy", confidence=0.8, amount_quote=amount, reasoning_nl="Kopen.")
    )


def sell_decider():
    return FakeDecider(
        TradeDecision(action="sell", confidence=0.8, amount_quote=0, reasoning_nl="Eruit.")
    )


# -- client: market validation, rate limits, caching (F48, F63, F73) ----------------------


@pytest.fixture
async def client():
    c = BitvavoClient("key", "secret", BASE)
    yield c
    await c.aclose()


@pytest.mark.parametrize(
    "market", ["../account", "BTC-EUR/../../x", "BTC-EUR?limit=1", "btc-eur", "BTC_EUR", ""]
)
async def test_client_rejects_bad_market_names_before_sending(client, market):
    with respx.mock(assert_all_called=False) as router:
        route = router.route()
        for call in (
            client.candles(market),
            client.ticker_price(market),
            client.place_order(market, "buy", amount_quote=10),
            client.get_order(market, client_order_id="x"),
        ):
            with pytest.raises(InvalidMarketError):
                await call
        assert not route.called


@respx.mock
async def test_client_backs_off_after_a_rate_limit(client):
    allow_at = int((time.time() + 120) * 1000)
    route = respx.get(f"{BASE}/time").mock(
        return_value=Response(
            429,
            json={"errorCode": 103, "error": "rate limited"},
            headers={"bitvavo-ratelimit-allowat": str(allow_at)},
        )
    )
    with pytest.raises(BitvavoError) as exc:
        await client.time()
    assert exc.value.rate_limited and client.blocked_until >= allow_at / 1000 - 1
    # The next calls are refused locally until the moment Bitvavo named, nothing is sent.
    with pytest.raises(BitvavoError, match="waiting after a Bitvavo rate limit"):
        await client.time()
    with pytest.raises(BitvavoError):
        await client.ticker_prices(["BTC-EUR"])
    assert route.call_count == 1


@respx.mock
async def test_client_honours_a_ban_expiry_in_the_message(client):
    expiry = int((time.time() + 600) * 1000)
    respx.get(f"{BASE}/time").mock(
        return_value=Response(
            403,
            json={"errorCode": 105, "error": f"Your IP has been banned. Expires at {expiry}"},
        )
    )
    with pytest.raises(BitvavoError):
        await client.time()
    assert client.blocked_until == pytest.approx(expiry / 1000, abs=1)


@respx.mock
async def test_client_keeps_the_last_weight_for_prices_and_orders(client):
    reset = int((time.time() + 50) * 1000)
    respx.get(f"{BASE}/time").mock(
        return_value=Response(
            200,
            json={"time": 1},
            headers={
                "bitvavo-ratelimit-remaining": "3",
                "bitvavo-ratelimit-resetat": str(reset),
            },
        )
    )
    candle_route = respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=[]))
    price_route = respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(200, json=[{"market": "BTC-EUR", "price": "1"}])
    )
    await client.time()
    assert client.rate_limit_remaining == 3
    with pytest.raises(BitvavoError, match="budget nearly used up"):
        await client.candles("BTC-EUR", "1h", 10)
    assert not candle_route.called
    assert await client.ticker_prices(["BTC-EUR"]) == {"BTC-EUR": 1.0}  # still allowed
    assert price_route.called


@respx.mock
async def test_client_caches_candles_briefly(client):
    route = respx.get(f"{BASE}/BTC-EUR/candles").mock(
        return_value=Response(200, json=[[1000, "1", "2", "0.5", "1.5", "5"]])
    )
    first = await client.candles("BTC-EUR", "1h", 10)
    first[0][4] = 999.0  # callers cannot change the cached copy
    assert (await client.candles("BTC-EUR", "1h", 10))[0][4] == 1.5
    assert route.call_count == 1
    await client.candles("BTC-EUR", "1h", 20)  # another window is another request
    assert route.call_count == 2


@respx.mock
async def test_client_network_error_is_a_bitvavo_error(client):
    respx.get(f"{BASE}/account").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(BitvavoNetworkError) as exc:
        await client.account()
    assert exc.value.ambiguous
    # The fee probe at startup is best-effort: offline means "unknown", not a crash (F73).
    assert await client.taker_fee_pct() is None


@respx.mock
async def test_client_sends_client_order_id_and_looks_it_up(client):
    post = respx.post(f"{BASE}/order").mock(
        return_value=Response(200, json={"orderId": "a", "status": "filled"})
    )
    get = respx.get(f"{BASE}/order").mock(
        return_value=Response(200, json={"orderId": "a", "status": "filled"})
    )
    await client.place_order("BTC-EUR", "sell", amount="0.0012", client_order_id="c-1")
    assert json.loads(post.calls.last.request.content) == {
        "market": "BTC-EUR",
        "side": "sell",
        "orderType": "market",
        "amount": "0.0012",
        "clientOrderId": "c-1",
    }
    await client.get_order("BTC-EUR", client_order_id="c-1")
    assert dict(get.calls.last.request.url.params) == {"market": "BTC-EUR", "clientOrderId": "c-1"}


async def test_startup_survives_an_unreachable_bitvavo(monkeypatch):
    from app.main import app

    async def offline(self):
        raise httpx.ConnectError("offline")  # even an error the probe itself does not catch

    monkeypatch.setattr(BitvavoClient, "authenticated", property(lambda self: True))
    monkeypatch.setattr(BitvavoClient, "taker_fee_pct", offline)
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None and app.state.broker.mode == "paper"


# -- live broker: write-ahead, real fills, timeouts (F34, F67, F69, F74) -------------------


async def test_live_order_is_written_as_pending_before_it_is_sent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'wal.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    exchange = FakeExchange({"BTC-EUR": 50_000.0})
    seen = []

    def check(market, side, kw):
        with factory() as other:  # another connection: only committed rows are visible
            row = other.scalar(select(Order).where(Order.client_order_id == kw["client_order_id"]))
            seen.append(row.status if row else None)
        return None

    exchange.script = [check]
    with factory() as s:
        order = await live_broker(exchange).market_order(s, "BTC-EUR", "buy", 100)
        s.commit()
    assert seen == ["pending"]
    assert order.status == "filled" and order.client_order_id
    assert exchange.placed[0]["client_order_id"] == order.client_order_id


async def test_live_timeout_after_execution_is_reconciled_not_repeated(session):
    exchange = FakeExchange({"BTC-EUR": 50_000.0})
    exchange.script = [lambda m, s, kw: ("lost", exchange.fill(m, s, kw))]
    order = await live_broker(exchange).market_order(session, "BTC-EUR", "buy", 100)
    # The POST timed out, but the lookup by clientOrderId found the executed order.
    assert order.status == "filled" and order.amount == pytest.approx(99.75 / 50_000)
    assert len(exchange.placed) == 1


async def test_live_timeout_without_answer_leaves_the_order_unknown(session):
    exchange = FakeExchange({"BTC-EUR": 50_000.0})
    exchange.script = [timeout()]
    with pytest.raises(OrderUncertain) as exc:
        await live_broker(exchange).market_order(session, "BTC-EUR", "buy", 100)
    order = exc.value.order
    assert order.status == "unknown"
    assert risk.has_unsettled_order(session, "live", "BTC-EUR")
    # Settled later: Bitvavo never got it, and after the grace period that is final.
    broker = live_broker(exchange)
    assert await broker.settle(session, order) is False  # too early to call it rejected
    order.created_at = datetime.now(UTC) - timedelta(minutes=5)
    assert await broker.settle(session, order) is True and order.status == "rejected"


async def test_live_refusal_is_recorded_as_rejected(session):
    exchange = FakeExchange({"BTC-EUR": 50_000.0})
    exchange.script = [BitvavoError(400, {"errorCode": 216, "error": "Insufficient balance"})]
    with pytest.raises(BitvavoError):
        await live_broker(exchange).market_order(session, "BTC-EUR", "buy", 100)
    (order,) = session.scalars(select(Order)).all()
    assert order.status == "rejected" and not risk.has_unsettled_order(session, "live", "BTC-EUR")


async def test_live_fee_in_the_coin_books_the_net_coins(session):
    exchange = FakeExchange({"BTC-EUR": 50_000.0})
    exchange.script = [
        {
            "orderId": "o1",
            "status": "filled",
            "filledAmount": "0.002",
            "filledAmountQuote": "100",
            "feePaid": "0.000005",
            "feeCurrency": "BTC",
        }
    ]
    order = await live_broker(exchange).market_order(session, "BTC-EUR", "buy", 100)
    assert order.amount == pytest.approx(0.002 - 0.000005)  # what really arrived
    assert order.amount_quote == pytest.approx(100.0)
    assert order.fee_quote == pytest.approx(0.000005 * 50_000)
    assert order.price == pytest.approx(50_000)


async def test_live_order_that_is_not_final_is_polled_and_the_rest_cancelled(session):
    exchange = FakeExchange({"BTC-EUR": 50_000.0})

    def slow(market, side, kw):
        resp = exchange.fill(market, side, kw, fraction=0.4, status="partiallyFilled")
        return resp

    exchange.script = [slow]
    order = await live_broker(exchange).market_order(session, "BTC-EUR", "buy", 100)
    assert exchange.cancelled == ["ex-1"]
    assert order.status == "canceled" and order.amount == pytest.approx(0.4 * 99.75 / 50_000)


async def test_live_sell_uses_base_amount_rounded_down_and_capped(session):
    exchange = FakeExchange(
        {"BTC-EUR": 50_000.0}, balance={"EUR": 0, "BTC": 0.00123456}, decimals=5
    )
    broker = live_broker(exchange)
    qty = await broker.sellable(session, "BTC-EUR", 0.002)  # more than the account holds
    assert qty == pytest.approx(0.00123)
    order = await broker.market_order(session, "BTC-EUR", "sell", amount=qty)
    assert exchange.placed[0]["amount"] == "0.00123" and "amount_quote" not in exchange.placed[0]
    assert order.amount == pytest.approx(0.00123)
    assert floor_amount(0.129999, 2) == floor_amount(0.12, 2)


async def test_paper_broker_refuses_non_finite_amounts(session):
    class Prices:
        async def ticker_price(self, market):
            return 100.0

    broker = PaperBroker(Prices(), start_eur=1000)
    for bad in (float("nan"), float("inf"), -5, 0):
        with pytest.raises(ValueError):
            await broker.market_order(session, "BTC-EUR", "buy", bad)
    assert await broker.balances(session) == {"EUR": 1000.0}


# -- engine with live fills: partial, canceled, reconcile (F67, F34, F74) ------------------


async def test_partially_filled_buy_opens_a_position_of_what_filled(session):
    exchange = FakeExchange({"BTC-EUR": 12.0}, {"BTC-EUR": candles(BUY_SERIES)})
    exchange.script = [
        lambda m, s, kw: exchange.fill(m, s, kw, fraction=0.3, status="canceledMarketProtection")
    ]
    engine = live_engine(session, exchange, buy_decider())
    (result,) = await engine.run_cycle(session)
    assert result.executed
    pos = open_position(session, "live", "BTC-EUR")
    assert pos.amount == pytest.approx(0.3 * 99.75 / 12) and pos.stop_price == pytest.approx(10.8)
    order = session.get(Order, pos.open_order_id)
    assert order.status == "canceledMarketProtection"  # 24 characters, fits now (F68)


async def test_unfilled_buy_opens_nothing(session):
    exchange = FakeExchange({"BTC-EUR": 12.0}, {"BTC-EUR": candles(BUY_SERIES)})
    exchange.script = [
        {"orderId": "x", "status": "canceled", "filledAmount": "0", "filledAmountQuote": "0"}
    ]
    engine = live_engine(session, exchange, buy_decider())
    (result,) = await engine.run_cycle(session)
    assert not result.executed and "niet gevuld" in result.reason
    assert session.scalars(select(Position)).all() == []


async def test_canceled_sell_keeps_the_position_open(session):
    exchange = FakeExchange({"BTC-EUR": 8.0}, balance={"EUR": 0, "BTC": 5})
    exchange.script = [
        {"orderId": "x", "status": "canceled", "filledAmount": "0", "filledAmountQuote": "0"}
    ]
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    pos = add_position(session, mode="live", stop=9.0)
    (result,) = await engine.run_exits(session)
    assert result.action == "sell" and not result.executed
    session.refresh(pos)
    assert pos.status == "open" and pos.amount == 5
    d = session.scalars(select(Decision)).one()
    assert d.source == "mechanical" and not d.executed and "niet gevuld" in d.blocked_by


async def test_partially_filled_sell_books_the_part_and_keeps_the_rest_protected(session):
    exchange = FakeExchange({"BTC-EUR": 8.0}, balance={"EUR": 0, "BTC": 5})
    exchange.script = [
        lambda m, s, kw: exchange.fill(m, s, kw, fraction=0.4, status="canceledMarketProtection")
    ]
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    pos = add_position(session, mode="live", stop=9.0)
    (result,) = await engine.run_exits(session)
    assert result.executed
    session.refresh(pos)
    assert pos.status == "open" and pos.amount == pytest.approx(3.0)
    assert pos.entry_quote == pytest.approx(30.0) and pos.stop_price == 9.0
    part = session.scalars(select(Position).where(Position.status == "closed")).one()
    assert part.amount == pytest.approx(2.0) and part.close_reason == "stop_loss"
    assert part.pnl_eur == pytest.approx(16 * 0.9975 - 20)
    assert exchange.placed[0]["amount"] == "5"  # base amount, not euros
    # The next exit check sells the rest.
    await engine.run_exits(session)
    session.refresh(pos)
    assert pos.status == "closed" and exchange.bal["BTC"] == pytest.approx(0)


async def test_sell_is_capped_at_the_coins_on_the_account(session):
    # The owner sold 1 of the 5 coins by hand on Bitvavo: the stop sells what is there.
    exchange = FakeExchange({"BTC-EUR": 8.0}, balance={"EUR": 0, "BTC": 4})
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    pos = add_position(session, mode="live", stop=9.0)
    await engine.run_exits(session)
    session.refresh(pos)
    assert exchange.placed[0]["amount"] == "4"
    assert pos.status == "closed" and pos.exit_quote == pytest.approx(32 * 0.9975)


async def test_buy_that_timed_out_is_settled_by_reconcile(session):
    exchange = FakeExchange({"BTC-EUR": 12.0}, {"BTC-EUR": candles(BUY_SERIES)})
    exchange.script = [timeout()]
    decider = buy_decider()
    engine = live_engine(session, exchange, decider)
    (result,) = await engine.run_cycle(session)
    assert not result.executed and "onbekend" in result.reason
    assert open_position(session, "live", "BTC-EUR") is None
    # The claim keeps every other buy out of this market meanwhile.
    claim = session.scalars(select(Position)).one()
    assert claim.status == "opening"
    (result,) = await engine.run_cycle(session)
    assert (
        len(decider.contexts) == 1
        and "order onderweg"
        in session.scalars(select(Decision).order_by(Decision.id.desc())).first().blocked_by
    )
    # Bitvavo did execute it after all; reconcile books the position with its stop.
    order = session.scalars(select(Order)).one()
    exchange.orders[order.client_order_id] = exchange.fill("BTC-EUR", "buy", {"amount_quote": 100})
    assert await engine.reconcile(session) == 1
    pos = open_position(session, "live", "BTC-EUR")
    assert pos is not None and pos.id == claim.id and pos.stop_price == pytest.approx(10.8)
    assert pos.amount == pytest.approx(99.75 / 12) and len(exchange.placed) == 1


async def test_reconcile_at_startup_settles_a_pending_sell(session):
    exchange = FakeExchange({"BTC-EUR": 8.0}, balance={"EUR": 0, "BTC": 0})
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    pos = add_position(session, mode="live", stop=9.0)
    pos.status = "closing"
    order = Order(
        mode="live",
        market="BTC-EUR",
        side="sell",
        order_type="market",
        status="pending",
        client_order_id="crashed-1",
        position_id=pos.id,
        reason="stop_loss",
    )
    session.add(order)
    session.commit()
    exchange.bal["BTC"] = 5
    exchange.orders["crashed-1"] = exchange.fill("BTC-EUR", "sell", {"amount": 5})
    assert await engine.reconcile(session) == 1
    session.refresh(pos)
    assert pos.status == "closed" and pos.close_reason == "stop_loss"
    assert order.status == "filled"


async def test_reconcile_releases_stale_claims(session):
    exchange = FakeExchange({"BTC-EUR": 8.0})
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    old = datetime.now(UTC) - timedelta(minutes=30)
    opening = add_position(session, mode="live", market="BTC-EUR", opened_at=old)
    opening.status = "opening"
    closing = add_position(session, mode="paper", market="BTC-EUR", opened_at=old)
    closing.status, closing.closed_at = "closing", old
    session.commit()
    await engine.reconcile(session)
    assert session.get(Position, opening.id) is None
    session.refresh(closing)
    assert closing.status == "open" and closing.closed_at is None


# -- refused orders: journal and backoff (F70) ---------------------------------------------


async def test_refused_live_buy_is_journaled_and_backs_off(session):
    exchange = FakeExchange({"BTC-EUR": 12.0}, {"BTC-EUR": candles(BUY_SERIES)})
    exchange.script = [BitvavoError(400, {"errorCode": 216, "error": "Insufficient balance"})]
    decider = buy_decider()
    engine = live_engine(session, exchange, decider)
    (result,) = await engine.run_cycle(session)
    assert not result.executed and "geweigerd" in result.reason
    d = session.scalars(select(Decision)).one()
    assert "Insufficient balance" in d.blocked_by and d.source == "claude"
    assert session.scalars(select(Position)).all() == []  # the claim was released
    # The next cycle does not pay Claude for the same refused order.
    (result,) = await engine.run_cycle(session)
    assert len(decider.contexts) == 1 and result.action == "hold"
    assert "geweigerde order" in result.reason
    # The backoff survives a restart.
    engine2 = live_engine(session, exchange, decider)
    engine2.load_state(session)
    assert "BTC-EUR" in engine2.order_backoff


# -- exit loop (F33, F63) ------------------------------------------------------------------


async def test_exit_loop_sells_regardless_of_kill_switch_and_markets(session):
    decider = FakeDecider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    engine.client.prices["SOL-EUR"] = 4.0
    risk.set_paused(session, True)  # the kill switch blocks buys only
    await engine.broker.market_order(session, "BTC-EUR", "buy", 100)  # coins in the wallet
    session.commit()
    wallet = await engine.broker.balances(session)
    wallet["SOL"] = 10
    session.get(Setting, "paper_wallet").value = json.dumps(wallet)
    session.commit()
    # SOL-EUR is no longer in MARKETS, its position must still be protected.
    sol = add_position(session, market="SOL-EUR", amount=10, entry=5.0, stop=4.5)
    btc = add_position(session, market="BTC-EUR", amount=5, entry=10.0, stop=9.0)
    results = await engine.run_exits(session)
    assert [r.market for r in results] == ["SOL-EUR"]
    session.refresh(sol)
    session.refresh(btc)
    assert sol.status == "closed" and sol.close_reason == "stop_loss"
    assert btc.status == "open"  # 10 is above its stop
    assert decider.contexts == [] and engine.unprotected == []


async def test_exit_loop_keeps_protecting_live_positions_after_a_switch_to_paper(session):
    decider = FakeDecider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 8.0}, decider)
    exchange = FakeExchange({"BTC-EUR": 8.0}, balance={"EUR": 0, "BTC": 5})
    engine.client = exchange
    live = live_broker(exchange)
    engine.broker_for_mode = lambda mode: live if mode == "live" else None
    pos = add_position(session, mode="live", stop=9.0)
    assert engine.mode == "paper"
    (result,) = await engine.run_exits(session)
    assert result.executed and exchange.placed[0]["side"] == "sell"
    session.refresh(pos)
    assert pos.status == "closed"
    order = session.get(Order, pos.close_order_id)
    assert order.mode == "live"


async def test_exit_loop_reports_positions_it_cannot_protect(session, caplog):
    decider = FakeDecider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 8.0}, decider)
    engine.broker_for_mode = lambda mode: None  # live no longer allowed by the environment
    pos = add_position(session, mode="live", stop=9.0)
    with caplog.at_level("ERROR"):
        assert await engine.run_exits(session) == []
    assert engine.unprotected == [
        {
            "id": pos.id,
            "mode": "live",
            "market": "BTC-EUR",
            "amount": 5.0,
            "stop_price": 9.0,
            "price": 8.0,
            "reason": "de modus kan nu niet handelen (live zonder Bitvavo-sleutel of "
            "LIVE_TRADING_ACK)",
        }
    ]
    assert "NOT protected" in caplog.text
    session.refresh(pos)
    assert pos.status == "open"


async def test_exit_scheduler_ignores_trading_enabled(monkeypatch):
    from app import main

    calls = []

    async def fake_periodically(app, name, job, seconds_between, **kw):
        calls.append(seconds_between)

    monkeypatch.setattr(main, "_run_periodically", fake_periodically)
    await main.exit_scheduler(None)
    settings = main.get_settings().model_copy(
        update={"trading_enabled": False, "exit_check_interval_min": 3}
    )
    assert calls[0](settings) == 180


# -- locks and the broker snapshot (F31, F81) ----------------------------------------------


class SlowDecider(FakeDecider):
    """Decides only when released, so two runs can overlap."""

    def __init__(self, decision, on_decide=None):
        super().__init__(decision)
        self.release = asyncio.Event()
        self.on_decide = on_decide

    async def decide(self, session, context):
        self.contexts.append(context)
        if self.on_decide:
            self.on_decide()
        await self.release.wait()
        return self.decision


async def test_overlapping_cycles_open_one_position(tmp_path):
    bind = make_engine(f"sqlite:///{tmp_path / 'locks.sqlite3'}")
    Base.metadata.create_all(bind)
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    decider = SlowDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=100, reasoning_nl="x")
    )
    with factory() as session, factory() as other:
        engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
        first = asyncio.create_task(engine.run_cycle(session))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(engine.run_cycle(other))  # e.g. the "cycle" button
        await asyncio.sleep(0.05)
        decider.release.set()
        (a,), (b,) = await asyncio.gather(first, second)
        assert a.executed and not b.executed
        assert len(decider.contexts) == 1  # the second run saw the open position: no Claude
        assert len(session.scalars(select(Position)).all()) == 1
        assert len(session.scalars(select(Order)).all()) == 1


async def test_mode_switch_during_a_run_keeps_the_snapshot_broker(session):
    exchange = FakeExchange({"BTC-EUR": 12.0})
    live = live_broker(exchange)
    holder = {}

    def switch():
        holder["engine"].broker = live  # the owner switches to live while Claude thinks

    decider = SlowDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=100, reasoning_nl="x"), switch
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    holder["engine"] = engine
    task = asyncio.create_task(engine.run_cycle(session))
    await asyncio.sleep(0.05)
    decider.release.set()
    (result,) = await task
    assert result.executed and exchange.placed == []  # no real order
    pos = session.scalars(select(Position)).one()
    assert pos.mode == "paper" and session.get(Order, pos.open_order_id).mode == "paper"
    assert session.scalars(select(Decision)).one().mode == "paper"


async def test_sell_refuses_a_broker_of_another_mode(session):
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 8.0}, None)
    pos = add_position(session, mode="live")
    with pytest.raises(RuntimeError, match="niet verkocht"):
        await engine._sell(session, engine.broker, pos, 8.0, "stop_loss")


async def test_database_allows_one_active_position_per_market(session):
    add_position(session, mode="paper", market="BTC-EUR")
    session.add(Position(mode="paper", market="BTC-EUR", amount=1, entry_price=1, entry_quote=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    # Another mode, or closed positions, are fine.
    add_position(session, mode="live", market="BTC-EUR")
    session.add(
        Position(
            mode="paper",
            market="BTC-EUR",
            status="closed",
            amount=1,
            entry_price=1,
            entry_quote=1,
        )
    )
    session.commit()


async def test_buy_is_refused_while_another_process_holds_the_market(session):
    decider = buy_decider()
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    claim = Position(
        mode="paper", market="BTC-EUR", status="opening", amount=0, entry_price=12, entry_quote=0
    )
    session.add(claim)  # e.g. the other container during a rolling deploy
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert not result.executed and "loopt al een positie" in result.reason
    assert session.scalars(select(Order)).all() == []


# -- manual orders (F18, F19) --------------------------------------------------------------


async def test_manual_orders_follow_the_rules(session):
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, None)
    for market, side, amount, why in (
        ("XRP-EUR", "buy", 50, "MARKETS"),
        ("BTC-EUR", "steal", 50, "kies kopen"),
        ("BTC-EUR", "buy", float("nan"), "geen geldig getal"),
        ("BTC-EUR", "buy", float("inf"), "geen geldig getal"),
        ("BTC-EUR", "buy", 4.99, "minimaal"),
        ("BTC-EUR", "buy", 100.01, "maximaal"),
        ("BTC-EUR", "sell", 50, "geen open positie"),
    ):
        with pytest.raises(ManualOrderError, match=why):
            await engine.manual_order(session, market, side, amount)
    risk.set_paused(session, True)
    with pytest.raises(ManualOrderError, match="kill switch"):
        await engine.manual_order(session, "BTC-EUR", "buy", 50)
    risk.set_paused(session, False)
    assert session.scalars(select(Order)).all() == []

    order = await engine.manual_order(session, "btc-eur", "buy", 50, reason="test")
    pos = open_position(session, "paper", "BTC-EUR")
    assert pos.open_order_id == order.id and pos.stop_price == pytest.approx(9.0)
    assert order.reason == "manual: test"
    with pytest.raises(ManualOrderError, match="al een open positie"):
        await engine.manual_order(session, "BTC-EUR", "buy", 20)

    # A partial manual sell reduces the bot's position, so its stop keeps working.
    await engine.manual_order(session, "BTC-EUR", "sell", 20)
    session.refresh(pos)
    assert pos.status == "open" and pos.amount == pytest.approx(3.0)
    closed = session.scalars(select(Position).where(Position.status == "closed")).one()
    assert closed.amount == pytest.approx(2.0) and closed.close_reason == "manual"
    # Selling (almost) all closes it; a rest below the exchange minimum goes with it.
    await engine.manual_order(session, "BTC-EUR", "sell", 27)
    session.refresh(pos)
    assert pos.status == "closed"
    assert "BTC" not in await engine.broker.balances(session)


async def test_manual_buy_respects_the_hard_limits(session):
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, None)
    engine.limits = LIMITS.__class__(**{**LIMITS.__dict__, "max_exposure_pct": 4})
    with pytest.raises(ManualOrderError, match="maximaal € 40.00"):
        await engine.manual_order(session, "BTC-EUR", "buy", 50)
    assert session.scalars(select(Order)).all() == []


@pytest.fixture
async def api():
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@respx.mock
async def test_order_endpoints_validate_input(api):
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(200, json=[{"market": "BTC-EUR", "price": "100"}])
    )
    r = await api.post(
        "/api/orders",
        content=b'{"market": "BTC-EUR", "side": "buy", "amount_quote": NaN}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400 and "geen geldig getal" in r.json()["detail"]
    r = await api.post("/api/orders", json={"market": "BTC-EUR", "side": "buy", "amount_quote": -5})
    assert r.status_code == 400 and "minimaal" in r.json()["detail"]
    r = await api.post(
        "/api/orders", json={"market": "BTC-EUR", "side": "short", "amount_quote": 10}
    )
    assert r.status_code == 422
    r = await api.post(
        "/api/orders", json={"market": "BTC-EUR", "side": "sell", "amount_quote": 10}
    )
    assert r.status_code == 400 and "geen open positie" in r.json()["detail"]
    r = await api.post("/orders", data={"market": "BTC-EUR", "side": "buy", "amount_quote": "inf"})
    assert r.status_code == 400 and "Order niet geplaatst" in r.text


# -- limits: daily loss incl. unrealized, bot capital (F20, F82) ---------------------------


def test_daily_loss_limit_counts_open_positions(session):
    add_position(session, mode="paper", amount=10, entry=10.0)  # 100 EUR in
    prices = {"BTC-EUR": 9.4}  # -6 EUR on paper
    unrealized = risk.unrealized_pnl(session, "paper", prices)
    assert unrealized == pytest.approx(-6.0)
    _, why = risk.check_buy(session, LIMITS, "paper", "ETH-EUR", 50, 100, 0, unrealized=unrealized)
    assert "dagverlies" in why and "ongerealiseerd" in why
    assert risk.check_buy(session, LIMITS, "paper", "ETH-EUR", 10, 1000, 0)[1] is None


async def test_live_capital_limits_exposure_to_the_bots_own_positions(session):
    exchange = FakeExchange({"BTC-EUR": 10.0}, balance={"EUR": 100_000.0, "ETH": 50})
    engine = live_engine(session, exchange, markets=["BTC-EUR"])
    assert await engine.portfolio(session, {"BTC-EUR": 10.0}) == (100_000.0, 0.0)
    engine.live_capital_eur = 500.0
    add_position(session, mode="live", amount=20, entry=10.0)
    assert await engine.portfolio(session, {"BTC-EUR": 12.0}) == (500.0, 240.0)


# -- news needs a second source (F36), Claude unavailable (F43) ----------------------------


def news(outlet, url, sentiment=0.9, key="etf", impact=3, source_name=None):
    return NewsItem(
        source="rss",
        source_name=source_name or outlet,
        url=url,
        title="Spot ETF approved",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        assessed=True,
        relevant=True,
        sentiment=sentiment,
        impact=impact,
        horizon="days",
        coins="BTC",
        event_key=key,
    )


def test_news_sources_counts_outlets_not_articles(session):
    session.add_all(
        [
            news("CoinDesk", "https://coindesk.com/a"),
            news("CoinDesk", "https://coindesk.com/b"),  # same outlet twice
            news("x", "https://news.example/a", source_name="Google News"),
        ]
    )
    session.commit()
    assert news_sources(session, "BTC", positive=True) == 2
    assert news_sources(session, "BTC", positive=False) == 0
    assert news_sources(session, "ETH", positive=True) == 0


async def test_single_source_news_does_not_trade(session):
    decider = buy_decider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    session.add_all([news("CoinDesk", "https://coindesk.com/a"), news("CoinDesk", "https://c/b")])
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and decider.contexts == []
    assert "niet bevestigd door een tweede bron" in result.reason


async def test_single_source_bad_news_does_not_sell(session):
    decider = sell_decider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    add_position(session, stop=None)
    session.add(news("CoinDesk", "https://coindesk.com/hack", sentiment=-0.9, key="hack"))
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and decider.contexts == []
    assert open_position(session, "paper", "BTC-EUR") is not None


async def test_fallback_hold_still_sells_on_a_strategy_signal(session):
    decider = FakeDecider(available=False)
    engine = make_engine_(session, {"BTC-EUR": candles(SELL_SERIES)}, {"BTC-EUR": 9.0}, decider)
    await engine.broker.market_order(session, "BTC-EUR", "buy", 90)
    session.commit()
    add_position(session, amount=10, entry=8.0, stop=1)
    (result,) = await engine.run_cycle(session)
    assert result.action == "sell" and result.executed and result.source == "rules"
    assert session.scalars(select(Position)).one().close_reason == "signal"


async def test_fallback_indicators_skips_buys_against_bad_news(session):
    decider = FakeDecider(available=False)
    engine = make_engine_(
        session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider, mode="indicators"
    )
    engine.shock_pause_hours = 0  # isolate the fallback from the shock pause
    session.add(news("CoinDesk", "https://c/x", sentiment=-0.8, key="bad", impact=2))
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and "negatief nieuws" in result.reason


# -- candles, deadlines, state (F79, F49, F72) ---------------------------------------------


def test_live_candle_window_fits_the_longest_indicator():
    assert live_candle_limit(cross_strategy()) == 300  # short periods: as before
    ema = cross_strategy(
        indicators=[
            {"type": "ema", "id": "fast", "period": 200},
            {"type": "sma", "id": "slow", "period": 4},
        ]
    )
    assert live_candle_limit(ema) == 402
    long = cross_strategy(
        indicators=[
            {"type": "drawdown", "id": "fast", "period": 900},
            {"type": "sma", "id": "slow", "period": 4},
        ]
    )
    assert live_candle_limit(long) == 1440


async def test_a_hanging_market_does_not_block_the_others(session):
    decider = FakeDecider()
    engine = make_engine_(
        session,
        {"ETH-EUR": candles(FLAT_SERIES), "BTC-EUR": candles(FLAT_SERIES)},
        {"ETH-EUR": 10.0, "BTC-EUR": 10.0},
        decider,
    )
    engine.data_timeout_s = 0.05
    original = engine.client.candles

    async def hang(market, interval, limit):
        if market == "ETH-EUR":
            await asyncio.sleep(10)
        return await original(market, interval, limit)

    engine.client.candles = hang
    eth, btc = await engine.run_cycle(session)
    assert eth.source == "error" and btc.source == "rules"


async def test_claude_deadline_falls_back(session):
    decider = SlowDecider(
        TradeDecision(action="buy", confidence=1, amount_quote=1, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(BUY_SERIES)}, {"BTC-EUR": 12.0}, decider)
    engine.decision_timeout_s = 0.05
    (result,) = await engine.run_cycle(session)
    assert result.action == "hold" and "langer dan" in engine.last_error


async def test_dip_watch_state_survives_a_restart(session):
    decider = FakeDecider()
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    now = datetime.now(UTC)
    engine.watch["BTC-EUR"] = {"since": now, "last_check": now, "dip": "rsi", "values": {"a": 1}}
    engine.watch_cooldown["ETH-EUR"] = now + timedelta(minutes=30)
    engine.save_state(session)
    fresh = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    fresh.load_state(session)
    assert fresh.watch["BTC-EUR"]["since"] == now and fresh.watch["BTC-EUR"]["dip"] == "rsi"
    assert fresh.watch_cooldown["ETH-EUR"] == now + timedelta(minutes=30)
    session.get(Setting, "engine_state").value = "{broken"
    session.commit()
    fresh.load_state(session)  # unreadable state is ignored, never a crash


async def test_scheduler_waits_for_the_next_due_time_after_a_restart(monkeypatch):
    from app import main
    from app.db import SessionLocal, init_db

    init_db()
    monkeypatch.setattr(main, "SCHEDULER_TICK_S", 0.01)
    runs = []

    async def job(session):
        runs.append(1)

    class FakeApp:
        class state:  # noqa: N801
            settings = None

    async def run_for(name, seconds=0.05):
        task = asyncio.create_task(
            main._run_periodically(FakeApp, name, job, lambda s: 3600, initial_delay=0)
        )
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        await run_for("test job")
        assert runs == [1]  # never ran before: runs at once
        await run_for("test job")
        assert runs == [1]  # "restart": the last run is in the database, so it waits
        main._save_last_run("test job", datetime.now(UTC) - timedelta(hours=2))
        await run_for("test job")
        assert runs == [1, 1]
    finally:
        with SessionLocal() as s:
            s.query(Setting).filter(Setting.key.like("sched:last:test job")).delete(
                synchronize_session=False
            )
            s.commit()


# -- database: widening, truncation, rollback per market (F68, F86) -----------------------


def _clear_trading_rows(s, keep_strategy: str | None) -> None:
    s.query(Decision).delete()
    s.query(Order).delete()
    s.query(Position).delete()
    s.query(Setting).filter(Setting.key.in_(["paper_wallet", "engine_state"])).delete(
        synchronize_session=False
    )
    row = s.get(Setting, "strategy")
    if keep_strategy is None and row is not None:
        s.delete(row)
    elif keep_strategy is not None:
        if row is None:
            s.add(Setting(key="strategy", value=keep_strategy))
        else:
            row.value = keep_strategy
    s.commit()


@pytest.fixture
def db():
    """A session on the configured database (SQLite in memory, or Postgres in CI), cleared of
    trading rows before and after, with the stored strategy put back afterwards."""
    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as s:
        row = s.get(Setting, "strategy")
        strategy = row.value if row is not None else None
        _clear_trading_rows(s, strategy)
        yield s
        s.rollback()
        _clear_trading_rows(s, strategy)


def test_widen_columns_grows_short_varchars(db):
    bind = db.get_bind()
    meta = MetaData()
    Table(
        "widen_probe",
        meta,
        Column("id", Integer, primary_key=True),
        Column("status", String(32)),
        Column("note", String(8)),
    )
    old = MetaData()
    Table(
        "widen_probe",
        old,
        Column("id", Integer, primary_key=True),
        Column("status", String(16)),
        Column("note", String(16)),
    )
    old.create_all(bind)
    try:
        widened = widen_columns(bind, meta)
        lengths = {
            c["name"]: getattr(c["type"], "length", None)
            for c in inspect(bind).get_columns("widen_probe")
        }
        if bind.dialect.name == "postgresql":
            assert widened == ["widen_probe.status"]
            assert lengths == {"id": None, "status": 32, "note": 16}  # never shrinks
            with bind.begin() as conn:
                conn.execute(
                    text("INSERT INTO widen_probe (status) VALUES (:s)"),
                    {"s": "canceledSelfTradePrevention"},
                )
        else:
            assert widened == []  # SQLite does not enforce lengths: nothing to do
        assert widen_columns(bind, meta) == []  # idempotent
    finally:
        old.drop_all(bind)


def test_existing_databases_get_the_new_columns_and_indexes():
    from app.db import add_missing_columns, add_missing_indexes

    bind = make_engine("sqlite:///:memory:")
    with bind.begin() as conn:  # tables as an older version created them
        conn.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, mode VARCHAR(8), market VARCHAR(16),"
                " side VARCHAR(4), order_type VARCHAR(16), status VARCHAR(16))"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE positions (id INTEGER PRIMARY KEY, mode VARCHAR(8), "
                "market VARCHAR(16), status VARCHAR(8), amount FLOAT, entry_price FLOAT, "
                "entry_quote FLOAT)"
            )
        )
    add_missing_columns(bind)
    add_missing_indexes(bind)
    add_missing_indexes(bind)  # idempotent
    insp = inspect(bind)
    assert {"client_order_id", "position_id"} <= {c["name"] for c in insp.get_columns("orders")}
    assert "uq_active_position" in {ix["name"] for ix in insp.get_indexes("positions")}
    assert widen_columns(bind) == []  # SQLite: no-op


def test_long_texts_are_clipped_to_their_column(db):
    d = Decision(
        mode="paper",
        market="BTC-EUR",
        price=1,
        signal="buy",
        source="rules",
        action="hold",
        blocked_by="risk-off: " + "nieuwsschok " * 40,
    )
    o = Order(
        mode="paper",
        market="BTC-EUR",
        side="buy",
        order_type="market",
        status="canceledSelfTradePrevention" + "x" * 20,
    )
    db.add_all([d, o])
    db.commit()  # would fail on Postgres with the old VARCHAR(16) / unclipped text
    assert len(d.blocked_by) == 128 and d.blocked_by.endswith("…")
    assert len(o.status) == 32


async def test_a_database_error_in_one_market_does_not_abort_the_others(db):
    decider = FakeDecider()
    engine = make_engine_(
        db,
        {"ETH-EUR": candles(FLAT_SERIES), "BTC-EUR": candles(FLAT_SERIES)},
        {"ETH-EUR": 10.0, "BTC-EUR": 10.0},
        decider,
    )
    original = engine._run_market

    async def broken(session, broker, strategy, market, prices, watch, consult=False):
        if market == "ETH-EUR":
            session.execute(text("SELECT * FROM no_such_table"))  # poisons a PG transaction
        return await original(session, broker, strategy, market, prices, watch, consult)

    engine._run_market = broken
    eth, btc = await engine.run_cycle(db)
    assert eth.source == "error" and btc.source == "rules"
    assert db.scalars(select(Decision)).one().market == "BTC-EUR"
