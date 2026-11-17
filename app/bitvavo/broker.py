"""Broker abstraction: the rest of the app talks to a Broker, never to Bitvavo directly.

``PaperBroker`` simulates fills at the live ticker price and keeps a virtual wallet in
the ``settings`` table. ``LiveBroker`` forwards to Bitvavo. Both record every order in
the ``orders`` table so the dashboard and later steps can audit what happened.

Brokers flush but do not commit the filled order: the caller commits it together with the
position and journal entry, in one transaction. A live order is the exception on the way
in: it is written as ``pending`` with its clientOrderId and committed *before* it is sent,
so an order whose response got lost can always be found again (see ``settle``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bitvavo.client import ORDER_NOT_FOUND_CODE, BitvavoClient, BitvavoError
from app.db import Order, Setting

log = logging.getLogger(__name__)

PAPER_WALLET_KEY = "paper_wallet"
# Bitvavo statuses that will not change any more. Everything else (new, partiallyFilled,
# awaitingTrigger) can still fill.
FINAL_STATUSES = ("filled", "expired", "rejected")
# How long a "not found" answer is trusted to mean "never placed" after the order was sent.
NOT_FOUND_GRACE = timedelta(minutes=2)


def is_final(status: str | None) -> bool:
    return bool(status) and (status in FINAL_STATUSES or status.startswith("canceled"))


class OrderUncertain(BitvavoError):
    """The order was sent but its outcome is not known; ``reconcile`` settles it later."""

    def __init__(self, order: Order, cause: object):
        super().__init__(
            0,
            {
                "error": f"order {order.client_order_id} in {order.market}: uitkomst nog onbekend "
                f"({cause}); wordt later gecontroleerd"
            },
        )
        self.order = order


def _num(value: Any) -> float:
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _check_amount(name: str, value: float | None) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} is required")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def floor_amount(amount: float, decimals: int) -> Decimal:
    """``amount`` rounded down to ``decimals`` places, never up (Bitvavo rejects too many)."""
    decimals = max(0, min(int(decimals), 12))
    return Decimal(repr(float(amount))).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


class Broker(Protocol):
    mode: str

    async def balances(self, session: Session) -> dict[str, float]: ...

    async def sellable(self, session: Session, market: str, amount: float) -> float: ...

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float | None = None,
        reason: str | None = None,
        *,
        amount: float | None = None,
        position_id: int | None = None,
    ) -> Order: ...

    async def settle(self, session: Session, order: Order) -> bool: ...


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
        session.flush()

    async def balances(self, session: Session) -> dict[str, float]:
        return self._load_wallet(session)

    async def sellable(self, session: Session, market: str, amount: float) -> float:
        held = self._load_wallet(session).get(market.split("-")[0], 0.0)
        return max(min(float(amount), held), 0.0)

    async def settle(self, session: Session, order: Order) -> bool:
        # Paper orders are booked in one go; a pending one can only be left by a crash
        # half-way, before the wallet changed.
        if order.status in ("pending", "unknown"):
            order.status = "rejected"
            session.commit()
        return True

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float | None = None,
        reason: str | None = None,
        *,
        amount: float | None = None,
        position_id: int | None = None,
    ) -> Order:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        base, quote = market.split("-")
        if side == "buy" or amount is None:
            amount_quote = _check_amount("amount_quote", amount_quote)
        else:
            amount = _check_amount("amount", amount)
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
            # Sell ``amount`` coins (or, legacy, coins worth amount_quote); the fee comes off
            # the EUR received.
            if amount is None:
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
            client_order_id=str(uuid.uuid4()),
            position_id=position_id,
            reason=reason,
        )
        session.add(order)
        self._save_wallet(session, wallet)
        return order


def apply_fill(order: Order, resp: dict[str, Any]) -> None:
    """Copy Bitvavo's order state onto ``order``: only what really filled, nothing assumed.

    ``amount`` becomes the coins that arrived (buy) or left the account (sell), after a fee
    charged in the coin itself; ``amount_quote`` the EUR that left (buy) or arrived (sell).
    """
    base, quote = order.market.split("-")
    filled = _num(resp.get("filledAmount"))
    filled_quote = _num(resp.get("filledAmountQuote"))
    fee = _num(resp.get("feePaid"))
    fee_currency = resp.get("feeCurrency") or quote
    price = filled_quote / filled if filled > 0 and filled_quote > 0 else None
    coins = filled
    fee_quote = 0.0
    eur_fee = 0.0
    if fee_currency == quote:
        fee_quote = eur_fee = fee
    elif fee_currency == base:
        # Fee taken in the coin: fewer coins arrive (buy) or more coins leave (sell).
        coins = filled - fee if order.side == "buy" else filled + fee
        fee_quote = fee * price if price else 0.0
    elif fee:
        log.warning(
            "order %s: fee of %s %s cannot be expressed in %s; not booked",
            order.client_order_id,
            fee,
            fee_currency,
            quote,
        )
    order.status = str(resp.get("status") or "unknown")
    order.exchange_order_id = resp.get("orderId") or order.exchange_order_id
    order.amount = max(coins, 0.0)
    if filled > 0:
        order.amount_quote = (
            filled_quote + eur_fee if order.side == "buy" else filled_quote - eur_fee
        )
    else:
        order.amount_quote = 0.0
    order.price = price
    order.fee_quote = fee_quote


class LiveBroker:
    mode = "live"
    poll_delay_s = 1.0  # between status checks of an order that is not final yet
    max_polls = 3

    def __init__(self, client: BitvavoClient):
        self._client = client

    async def balances(self, session: Session) -> dict[str, float]:
        rows = await self._client.balance()
        return {
            r["symbol"]: float(r["available"]) + float(r.get("inOrder", 0))
            for r in rows
            if float(r["available"]) + float(r.get("inOrder", 0)) > 0
        }

    async def _decimals(self, market: str) -> int:
        """Quantity precision of ``market`` from Bitvavo's markets endpoint (8 when unknown)."""
        try:
            info = await self._client.market_info(market) or {}
        except BitvavoError as exc:
            log.warning("market info for %s unavailable (%s); using 8 decimals", market, exc)
            return 8
        try:
            return int(info.get("quantityDecimals", 8))
        except (TypeError, ValueError):
            return 8

    async def sellable(self, session: Session, market: str, amount: float) -> float:
        """``amount`` capped at the coins available on the account, rounded down to the
        market's quantity precision."""
        base = market.split("-")[0]
        available = 0.0
        for row in await self._client.balance():
            if row.get("symbol") == base:
                available = _num(row.get("available"))
        qty = min(float(amount), available)
        if qty <= 0:
            return 0.0
        return float(floor_amount(qty, await self._decimals(market)))

    async def _lookup(self, order: Order) -> dict[str, Any] | None:
        """The exchange's view of ``order``, or None when Bitvavo does not know it."""
        try:
            return await self._client.get_order(
                order.market,
                order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
            )
        except BitvavoError as exc:
            if exc.code == ORDER_NOT_FOUND_CODE or exc.status == 404:
                return None
            raise

    async def _until_final(self, order: Order, resp: dict[str, Any]) -> dict[str, Any]:
        """Poll a market order that is not final yet; cancel what is left after a few tries."""
        for _ in range(self.max_polls):
            if is_final(resp.get("status")):
                return resp
            await asyncio.sleep(self.poll_delay_s)
            order.exchange_order_id = resp.get("orderId") or order.exchange_order_id
            resp = await self._lookup(order) or resp
        if not is_final(resp.get("status")) and resp.get("orderId"):
            try:
                await self._client.cancel_order(order.market, resp["orderId"])
            except BitvavoError as exc:
                log.warning("cancel of order %s failed: %s", resp.get("orderId"), exc)
            resp = await self._lookup(order) or resp
        return resp

    async def market_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: float | None = None,
        reason: str | None = None,
        *,
        amount: float | None = None,
        position_id: int | None = None,
    ) -> Order:
        if side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        kwargs: dict[str, Any]
        if side == "sell" and amount is not None:
            # Sell coins, not euros: an EUR amount at a stale price asks for more coins than
            # there are once the price dropped, exactly when a stop-loss must work.
            qty = floor_amount(_check_amount("amount", amount), await self._decimals(market))
            if qty <= 0:
                raise ValueError(f"amount {amount} is below the precision of {market}")
            text = format(qty, "f")
            kwargs = {"amount": text.rstrip("0").rstrip(".") if "." in text else text}
        else:
            kwargs = {"amount_quote": _check_amount("amount_quote", amount_quote)}
        order = Order(
            mode=self.mode,
            market=market,
            side=side,
            order_type="market",
            status="pending",
            client_order_id=str(uuid.uuid4()),
            position_id=position_id,
            reason=reason,
        )
        # Write-ahead: the order exists in the database before Bitvavo can execute it.
        session.add(order)
        session.commit()
        try:
            resp = await self._client.place_order(
                market,
                side,
                order_type="market",
                client_order_id=order.client_order_id,
                **kwargs,
            )
        except BitvavoError as exc:
            if not exc.ambiguous:
                order.status = "rejected"
                session.commit()
                raise
            # No (clear) answer: Bitvavo may have executed it. Ask, do not assume.
            log.warning("order %s: %s; looking it up", order.client_order_id, exc)
            try:
                resp = await self._lookup(order)
            except BitvavoError as lookup_exc:
                resp = None
                exc = lookup_exc
            if resp is None:
                order.status = "unknown"
                session.commit()
                raise OrderUncertain(order, exc) from exc
        try:
            resp = await self._until_final(order, resp)
        except BitvavoError as exc:
            log.warning("order %s: status check failed: %s", order.client_order_id, exc)
        if not is_final(resp.get("status")):
            order.status = "unknown"
            order.exchange_order_id = resp.get("orderId") or order.exchange_order_id
            session.commit()
            raise OrderUncertain(order, f"status {resp.get('status')}")
        apply_fill(order, resp)
        session.flush()
        return order

    async def settle(self, session: Session, order: Order) -> bool:
        """Settle a pending/unknown order from the exchange. True when it is final now."""
        resp = await self._lookup(order)
        if resp is None:
            created = order.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created is None or datetime.now(UTC) - created > NOT_FOUND_GRACE:
                order.status = "rejected"  # Bitvavo never got it
                session.commit()
                return True
            return False
        resp = await self._until_final(order, resp)
        if not is_final(resp.get("status")):
            return False
        apply_fill(order, resp)
        session.flush()
        return True


def recent_orders(
    session: Session, limit: int = 50, mode: str | None = None, before_id: int | None = None
) -> list[Order]:
    """The newest orders first. ``mode`` ("paper" or "live") keeps one mode apart from the
    other, like the positions and the charts do: the overview never shows fictitious orders
    next to real ones. ``before_id``: only orders older than that one (the next page)."""
    stmt = select(Order)
    if mode is not None:
        stmt = stmt.where(Order.mode == mode)
    if before_id is not None:
        stmt = stmt.where(Order.id < before_id)
    return list(session.scalars(stmt.order_by(Order.id.desc()).limit(limit)))
