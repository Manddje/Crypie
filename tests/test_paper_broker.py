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


async def test_buy_then_sell_round_trip(session):
    broker = PaperBroker(FakeClient(50_000.0), start_eur=1000)
    assert await broker.balances(session) == {"EUR": 1000.0}

    buy = await broker.market_order(session, "BTC-EUR", "buy", 500, reason="test")
    assert buy.status == "filled" and buy.mode == "paper"
    assert buy.amount == pytest.approx(0.01)
    wallet = await broker.balances(session)
    assert wallet["EUR"] == pytest.approx(500) and wallet["BTC"] == pytest.approx(0.01)

    broker._client.price = 60_000.0
    sell = await broker.market_order(session, "BTC-EUR", "sell", 600)
    assert sell.amount == pytest.approx(0.01)
    wallet = await broker.balances(session)
    assert wallet["EUR"] == pytest.approx(1100)
    assert "BTC" not in wallet

    assert [o.side for o in recent_orders(session)] == ["sell", "buy"]


async def test_insufficient_funds_rejected(session):
    broker = PaperBroker(FakeClient(50_000.0), start_eur=100)
    with pytest.raises(ValueError, match="Insufficient EUR"):
        await broker.market_order(session, "BTC-EUR", "buy", 500)
    with pytest.raises(ValueError, match="Insufficient BTC"):
        await broker.market_order(session, "BTC-EUR", "sell", 10)
    assert await broker.balances(session) == {"EUR": 100.0}
