import pytest
from sqlalchemy.orm import sessionmaker

from app.bitvavo.broker import PaperBroker, recent_orders
from app.db import Base, make_engine


class FakeClient:
    def __init__(self, price: float):
        self.price = price

    async def ticker_price(self, market: str) -> float:
        return self.price


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


async def test_buy_then_sell_round_trip_without_fees(session):
    broker = PaperBroker(FakeClient(50_000.0), start_eur=1000, fee_pct=0)
    assert await broker.balances(session) == {"EUR": 1000.0}

    buy = await broker.market_order(session, "BTC-EUR", "buy", 500, reason="test")
    assert buy.status == "filled" and buy.mode == "paper"
    assert buy.amount == pytest.approx(0.01) and buy.fee_quote == 0
    wallet = await broker.balances(session)
    assert wallet["EUR"] == pytest.approx(500) and wallet["BTC"] == pytest.approx(0.01)

    broker._client.price = 60_000.0
    sell = await broker.market_order(session, "BTC-EUR", "sell", 600)
    assert sell.amount == pytest.approx(0.01)
    wallet = await broker.balances(session)
    assert wallet["EUR"] == pytest.approx(1100)
    assert "BTC" not in wallet

    assert [o.side for o in recent_orders(session)] == ["sell", "buy"]


async def test_fees_are_charged_like_bitvavo(session):
    # 0.25% taker fee per side, taken in EUR, like a Bitvavo market order.
    broker = PaperBroker(FakeClient(50_000.0), start_eur=1000)  # default fee 0.25%
    buy = await broker.market_order(session, "BTC-EUR", "buy", 500)
    assert buy.fee_quote == pytest.approx(1.25)
    assert buy.amount_quote == pytest.approx(500)  # EUR that left the wallet
    assert buy.amount == pytest.approx(498.75 / 50_000)  # fewer coins than fee-free
    wallet = await broker.balances(session)
    assert wallet["EUR"] == pytest.approx(500) and wallet["BTC"] == pytest.approx(0.009975)

    # Sell everything at the same price: the round trip costs ~0.5%.
    sell = await broker.market_order(session, "BTC-EUR", "sell", wallet["BTC"] * 50_000)
    assert sell.fee_quote == pytest.approx(498.75 * 0.0025)
    assert sell.amount_quote == pytest.approx(498.75 * 0.9975)  # net EUR received
    wallet = await broker.balances(session)
    assert "BTC" not in wallet
    assert wallet["EUR"] == pytest.approx(1000 - 500 + 498.75 * 0.9975)
    assert 1000 - wallet["EUR"] == pytest.approx(2.496875)  # total fees of the round trip


async def test_live_broker_books_bitvavo_fee():
    from app.bitvavo.broker import LiveBroker

    class FakeLive:
        def __init__(self):
            self.calls = []

        async def place_order(self, market, side, order_type="market", **kw):
            self.calls.append((market, side, kw))
            if side == "buy":
                return {
                    "orderId": "o1",
                    "status": "filled",
                    "filledAmount": "0.00199500",
                    "filledAmountQuote": "99.75",
                    "feePaid": "0.25",
                    "feeCurrency": "EUR",
                }
            return {
                "orderId": "o2",
                "status": "filled",
                "filledAmount": "0.00199500",
                "filledAmountQuote": "109.72",
                "feePaid": "0.27",
                "feeCurrency": "EUR",
            }

    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        broker = LiveBroker(FakeLive())
        buy = await broker.market_order(s, "BTC-EUR", "buy", 100)
        assert buy.fee_quote == pytest.approx(0.25)
        assert buy.amount_quote == pytest.approx(100.0)  # 99.75 traded + 0.25 fee
        assert buy.price == pytest.approx(50_000)
        sell = await broker.market_order(s, "BTC-EUR", "sell", 109.72)
        assert sell.amount_quote == pytest.approx(109.45)  # net after fee
        assert sell.fee_quote == pytest.approx(0.27)


async def test_insufficient_funds_rejected(session):
    broker = PaperBroker(FakeClient(50_000.0), start_eur=100)
    with pytest.raises(ValueError, match="Insufficient EUR"):
        await broker.market_order(session, "BTC-EUR", "buy", 500)
    with pytest.raises(ValueError, match="Insufficient BTC"):
        await broker.market_order(session, "BTC-EUR", "sell", 10)
    assert await broker.balances(session) == {"EUR": 100.0}
