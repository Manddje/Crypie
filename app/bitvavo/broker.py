"""Broker abstraction: the rest of the app talks to a Broker, never to Bitvavo directly.

``PaperBroker`` simulates fills at the live ticker price and keeps a virtual wallet in
the ``settings`` table. ``LiveBroker`` forwards to Bitvavo. Both record every order in
the ``orders`` table so the dashboard and later steps can audit what happened.
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bitvavo.client import BitvavoClient
from app.db import Order, Setting

PAPER_WALLET_KEY = "paper_wallet"


class Broker(Protocol):
    mode: str

    async def balances(self, session: Session) -> dict[str, float]: ...

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float,
        reason: str | None = None,
    ) -> Order: ...


class PaperBroker:
    mode = "paper"

    def __init__(self, client: BitvavoClient, start_eur: float, fee_pct: float = 0.25):
        self._client = client
        self._start_eur = start_eur
        self.fee_pct = fee_pct  # taker fee per side, like Bitvavo charges in EUR

    def _load_wallet(self, session: Session) -> dict[str, float]:
        row = session.get(Setting, PAPER_WALLET_KEY)
        if row is None:
            wallet = {"EUR": float(self._start_eur)}
            session.add(Setting(key=PAPER_WALLET_KEY, value=json.dumps(wallet)))
            session.commit()
            return wallet
        return {k: float(v) for k, v in json.loads(row.value).items()}

    def _save_wallet(self, session: Session, wallet: dict[str, float]) -> None:
        row = session.get(Setting, PAPER_WALLET_KEY)
        assert row is not None
        row.value = json.dumps({k: v for k, v in wallet.items() if abs(v) > 1e-12})
        session.commit()

    async def balances(self, session: Session) -> dict[str, float]:
        return self._load_wallet(session)

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float,
        reason: str | None = None,
    ) -> Order:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if amount_quote <= 0:
            raise ValueError("amount_quote must be positive")
        base, quote = market.split("-")
        price = await self._client.ticker_price(market)
        fee_rate = self.fee_pct / 100.0
        wallet = self._load_wallet(session)

        if side == "buy":
            # amount_quote is the total EUR leaving the wallet; the fee is taken from it,
            # so fewer coins arrive. Mirrors a Bitvavo market buy with amountQuote.
            if wallet.get(quote, 0.0) < amount_quote:
                raise ValueError(f"Insufficient {quote}: have {wallet.get(quote, 0.0):.2f}")
            fee = amount_quote * fee_rate
            amount = (amount_quote - fee) / price
            wallet[quote] = wallet.get(quote, 0.0) - amount_quote
            wallet[base] = wallet.get(base, 0.0) + amount
            net_quote = amount_quote
        else:
            # amount_quote is the gross value sold; the fee comes off the EUR received.
            amount = amount_quote / price
            held = wallet.get(base, 0.0)
            if held < amount:
                if held > 0 and amount - held < held * 1e-6:
                    amount = held  # rounding noise: sell what is left
                else:
                    raise ValueError(f"Insufficient {base}: have {held:.8f}")
            gross = amount * price
            fee = gross * fee_rate
            net_quote = gross - fee
            wallet[base] = held - amount
            wallet[quote] = wallet.get(quote, 0.0) + net_quote

        order = Order(
            mode=self.mode,
            market=market,
            side=side,
            order_type="market",
            amount=amount,
            amount_quote=net_quote,
            price=price,
            fee_quote=fee,
            status="filled",
            exchange_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            reason=reason,
        )
        session.add(order)
        self._save_wallet(session, wallet)
        session.refresh(order)
        return order


class LiveBroker:
    mode = "live"

    def __init__(self, client: BitvavoClient):
        self._client = client

    async def balances(self, session: Session) -> dict[str, float]:
        rows = await self._client.balance()
        return {
            r["symbol"]: float(r["available"]) + float(r.get("inOrder", 0))
            for r in rows
            if float(r["available"]) + float(r.get("inOrder", 0)) > 0
        }

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float,
        reason: str | None = None,
    ) -> Order:
        resp = await self._client.place_order(
            market, side, order_type="market", amount_quote=amount_quote
        )
        filled_amount = float(resp.get("filledAmount") or 0) or None
        filled_quote = float(resp.get("filledAmountQuote") or 0) or amount_quote
        price = (filled_quote / filled_amount) if filled_amount else None
        # Bitvavo reports the fee separately (feePaid in feeCurrency, EUR on EUR markets).
        # amount_quote below is what actually left (buy) or arrived in (sell) the EUR balance.
        fee = float(resp.get("feePaid") or 0)
        _, quote = market.split("-")
        if resp.get("feeCurrency", quote) != quote:
            fee = 0.0  # fee paid in another currency; not expressible in EUR here
        net_quote = filled_quote + fee if side == "buy" else filled_quote - fee
        order = Order(
            mode=self.mode,
            market=market,
            side=side,
            order_type="market",
            amount=filled_amount,
            amount_quote=net_quote,
            price=price,
            fee_quote=fee,
            status=resp.get("status", "unknown"),
            exchange_order_id=resp.get("orderId"),
            reason=reason,
        )
        session.add(order)
        session.commit()
        session.refresh(order)
        return order


def recent_orders(session: Session, limit: int = 50) -> list[Order]:
    return list(session.scalars(select(Order).order_by(Order.id.desc()).limit(limit)))
