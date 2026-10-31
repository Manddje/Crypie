"""The trading loop: one cycle per market combines strategy, news, Claude and risk limits."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.decision_maker import DecisionMaker, TradeDecision
from app.ai.evaluator import Evaluator
from app.bitvavo.broker import Broker
from app.bitvavo.client import BitvavoClient
from app.db import Decision, Evaluation, Position
from app.news.service import market_sentiment
from app.strategy.describe import explain_signal, signal_reason_nl
from app.strategy.engine import backtest, compute_series, signal_at
from app.strategy.model import Strategy
from app.strategy.store import load_strategy
from app.trading import risk
from app.trading.risk import RiskLimits

log = logging.getLogger(__name__)

# News alone may trigger a Claude consult: a strongly positive, high-impact story can open a
# (smaller) position, a strongly negative one can close it. Scores run from -1 to 1.
NEWS_ENTRY_MIN_SCORE = 0.4
NEWS_EXIT_MAX_SCORE = -0.3
NEWS_ENTRY_SIZE_FACTOR = 0.5  # also used when a soft strategy filter does not hold


# -- positions ---------------------------------------------------------------------------


def open_position(session: Session, mode: str, market: str) -> Position | None:
    return session.scalar(
        select(Position).where(
            Position.mode == mode, Position.market == market, Position.status == "open"
        )
    )


def open_positions(session: Session, mode: str) -> list[Position]:
    return list(
        session.scalars(select(Position).where(Position.mode == mode, Position.status == "open"))
    )


def closed_positions(
    session: Session, mode: str, days: int = 30, limit: int = 200
) -> list[Position]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return list(
        session.scalars(
            select(Position)
            .where(Position.mode == mode, Position.status == "closed", Position.closed_at >= cutoff)
            .order_by(Position.closed_at.desc())
            .limit(limit)
        )
    )


def recent_decisions(
    session: Session, mode: str, limit: int = 50, days: int | None = None
) -> list[Decision]:
    stmt = select(Decision).where(Decision.mode == mode)
    if days is not None:
        stmt = stmt.where(Decision.created_at >= datetime.now(UTC) - timedelta(days=days))
    return list(session.scalars(stmt.order_by(Decision.id.desc()).limit(limit)))


def latest_decisions(session: Session, mode: str, markets: list[str]) -> list[Decision]:
    """The most recent journal entry per market, in the order of ``markets``."""
    out = []
    for market in markets:
        row = session.scalars(
            select(Decision)
            .where(Decision.mode == mode, Decision.market == market)
            .order_by(Decision.id.desc())
            .limit(1)
        ).first()
        if row is not None:
            out.append(row)
    return out


def performance_summary(session: Session, mode: str, days: int = 30) -> dict[str, Any]:
    closed = closed_positions(session, mode, days)
    wins = [p for p in closed if (p.pnl_eur or 0) > 0]
    decisions = recent_decisions(session, mode, 30)
    recent = []
    for d in decisions:
        if d.action == "hold" and d.source == "rules":
            continue
        entry: dict[str, Any] = {
            "when": d.created_at.strftime("%Y-%m-%d %H:%M"),
            "market": d.market,
            "signal": d.signal,
            "news": d.news_label,
            "action": d.action,
            "executed": d.executed,
        }
        if d.price_1h:
            entry["move_1h_pct"] = round((d.price_1h / d.price - 1) * 100, 2)
        if d.price_24h:
            entry["move_24h_pct"] = round((d.price_24h / d.price - 1) * 100, 2)
        if d.price_72h:
            entry["move_72h_pct"] = round((d.price_72h / d.price - 1) * 100, 2)
        if d.blocked_by:
            entry["blocked_by"] = d.blocked_by
        recent.append(entry)
    return {
        "closed_positions": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "total_pnl_eur": round(sum(p.pnl_eur or 0 for p in closed), 2),
        "total_fees_eur": round(sum(p.fees_eur or 0 for p in closed), 2),
        "avg_pnl_pct": round(sum(p.pnl_pct or 0 for p in closed) / len(closed), 2)
        if closed
        else None,
        "best_pct": round(max((p.pnl_pct or 0) for p in closed), 2) if closed else None,
        "worst_pct": round(min((p.pnl_pct or 0) for p in closed), 2) if closed else None,
        "recent_decisions": recent[:15],
    }


# -- engine ------------------------------------------------------------------------------


@dataclass
class CycleResult:
    market: str
    price: float
    signal: str
    action: str
    source: str
    executed: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class TradingEngine:
    def __init__(
        self,
        client: BitvavoClient,
        broker: Broker,
        decision_maker: DecisionMaker,
        evaluator: Evaluator,
        markets: list[str],
        limits: RiskLimits,
        claude_unavailable_mode: str = "hold",
        backtest_fee_pct: float = 0.25,
        fee_pct: float = 0.25,
    ):
        self.client = client
        self.broker = broker
        self.decision_maker = decision_maker
        self.evaluator = evaluator
        self.markets = markets
        self.limits = limits
        self.claude_unavailable_mode = claude_unavailable_mode
        self.backtest_fee_pct = backtest_fee_pct
        self.fee_pct = fee_pct  # per side; what a round trip must beat to break even is 2x
        self.last_run: datetime | None = None
        self.last_results: list[dict[str, Any]] = []
        self.last_error: str | None = None

    @property
    def mode(self) -> str:
        return self.broker.mode

    # -- portfolio helpers --

    async def portfolio(self, session: Session, prices: dict[str, float]) -> tuple[float, float]:
        """Return (total_value_eur, exposure_eur) based on broker balances."""
        balances = await self.broker.balances(session)
        total = balances.get("EUR", 0.0)
        exposure = 0.0
        for symbol, amount in balances.items():
            if symbol == "EUR":
                continue
            price = prices.get(f"{symbol}-EUR")
            if price:
                total += amount * price
                exposure += amount * price
        return total, exposure

    # -- one cycle --

    async def run_cycle(self, session: Session) -> list[CycleResult]:
        strategy = load_strategy(session)
        prices = await self.client.ticker_prices(self.markets)
        results: list[CycleResult] = []
        for market in self.markets:
            try:
                results.append(await self.run_market(session, strategy, market, prices))
            except Exception as exc:  # one market must not stop the others
                log.exception("cycle failed for %s", market)
                results.append(
                    CycleResult(
                        market, prices.get(market, 0.0), "error", "hold", "error", False, str(exc)
                    )
                )
        await self.update_outcomes(session, prices)
        self.last_run = datetime.now(UTC)
        self.last_results = [r.as_dict() for r in results]
        return results

    async def run_market(
        self, session: Session, strategy: Strategy, market: str, prices: dict[str, float]
    ) -> CycleResult:
        candles = await self.client.candles(market, strategy.interval, 300)
        series = compute_series(strategy, candles)
        i = len(candles) - 1
        signal = signal_at(strategy, series, i)
        checks = explain_signal(strategy, series, i)
        signal_reason = signal_reason_nl(signal, checks)
        price = prices.get(market) or candles[-1][4]
        indicators = {
            k: round(v[i], 6)
            for k, v in series.items()
            if k not in ("open", "high", "low", "volume") and v[i] is not None
        }
        coin = market.split("-")[0]
        sentiment = market_sentiment(session, coin, 48)
        position = open_position(session, self.mode, market)

        decision = Decision(
            mode=self.mode,
            market=market,
            price=price,
            signal=signal,
            indicators=json.dumps(indicators),
            checks=json.dumps(
                {k: [c.as_dict() for c in v] for k, v in checks.items()}, ensure_ascii=False
            ),
            news_score=sentiment.score,
            news_label=sentiment.label(),
            has_position=position is not None,
            source="rules",
            action="hold",
        )

        # 1. Mechanical exits first: stop-loss / take-profit on the live price.
        if position is not None:
            reason = None
            if position.stop_price and price <= position.stop_price:
                reason = "stop_loss"
            elif position.take_profit_price and price >= position.take_profit_price:
                reason = "take_profit"
            if reason:
                order = await self._close(session, position, price, reason)
                decision.source, decision.action, decision.executed = "mechanical", "sell", True
                decision.reasoning = f"{reason} geraakt op {price:.2f}"
                decision.order_id = order.id
                session.add(decision)
                session.commit()
                return CycleResult(market, price, signal, "sell", "mechanical", True, reason)

        # 2. Nothing to decide? Save the Claude call.
        news_entry = (
            position is None
            and signal == "hold"
            and sentiment.high_impact > 0
            and sentiment.score >= NEWS_ENTRY_MIN_SCORE
        )
        needs_claude = (
            signal == "buy"
            and position is None
            or signal == "sell"
            and position is not None
            or position is not None
            and sentiment.high_impact > 0
            and sentiment.score <= NEWS_EXIT_MAX_SCORE
            or news_entry
        )
        if not needs_claude:
            news = (
                "geen beoordeeld nieuws"
                if sentiment.items == 0
                else f"nieuws {sentiment.label()} ({sentiment.score:+.2f}, "
                f"{sentiment.items} berichten, {sentiment.high_impact} van hoge impact)"
            )
            if position is not None:
                why = "positie blijft open: geen verkoopsignaal en geen zwaar negatief nieuws"
            elif signal == "sell":
                why = "verkoopsignaal zonder open positie, niets te verkopen"
            else:
                why = "geen reden om Claude te raadplegen"
            decision.reasoning = (
                f"{signal_reason} {news[0].upper() + news[1:]}. {why[0].upper() + why[1:]}."
            )
            session.add(decision)
            session.commit()
            return CycleResult(market, price, signal, "hold", "rules", False, decision.reasoning)

        # 3. Ask Claude, or fall back.
        action, confidence, amount_quote, reasoning, source, model = await self._decide(
            session,
            strategy,
            market,
            price,
            signal,
            indicators,
            sentiment,
            position,
            news_entry,
            checks,
        )
        decision.source, decision.model = source, model
        decision.action, decision.confidence, decision.reasoning = action, confidence, reasoning
        soft_failed = any(c.soft and not c.ok for c in checks["buy"])
        if action == "buy" and (news_entry or soft_failed):
            # Without the full strategy behind it (news alone, or a soft filter such as the
            # trend that does not hold) a buy gets at most half the budget.
            amount_quote = min(amount_quote, self.limits.max_trade_eur * NEWS_ENTRY_SIZE_FACTOR)
        decision.amount_quote = amount_quote

        # 4. Enforce the rules and hard limits, then execute.
        executed = False
        blocked = None
        order_id = None
        if action == "buy":
            if position is not None:
                blocked = "kopen met een open positie is niet toegestaan"
            elif signal != "buy" and not news_entry:
                blocked = "kopen zonder buy-signaal of sterk positief nieuws is niet toegestaan"
            else:
                total, exposure = await self.portfolio(session, prices)
                allowed, blocked = risk.check_buy(
                    session, self.limits, self.mode, market, amount_quote, total, exposure
                )
                if blocked is None:
                    try:
                        order = await self.broker.market_order(
                            session, market, "buy", allowed, reason=f"{source}: {reasoning[:120]}"
                        )
                    except ValueError as exc:
                        blocked = str(exc)
                    else:
                        fill = order.price or price
                        spent = order.amount_quote or allowed  # EUR gone incl. fee
                        session.add(
                            Position(
                                mode=self.mode,
                                market=market,
                                amount=order.amount or allowed / fill,
                                entry_price=fill,
                                entry_quote=spent,
                                fees_eur=order.fee_quote or 0.0,
                                stop_price=fill * (1 - strategy.stop_loss_pct / 100)
                                if strategy.stop_loss_pct
                                else None,
                                take_profit_price=fill * (1 + strategy.take_profit_pct / 100)
                                if strategy.take_profit_pct
                                else None,
                                open_order_id=order.id,
                            )
                        )
                        decision.amount_quote = spent
                        executed, order_id = True, order.id
        elif action == "sell":
            if position is None:
                blocked = "geen open positie om te verkopen"
            elif risk.is_paused(session) and signal != "sell":
                blocked = "handelen is gepauzeerd (kill switch)"
            else:
                order = await self._close(
                    session, position, price, "signal" if signal == "sell" else "news_exit"
                )
                executed, order_id = True, order.id
        decision.executed, decision.blocked_by, decision.order_id = executed, blocked, order_id
        session.add(decision)
        session.commit()
        return CycleResult(market, price, signal, action, source, executed, blocked or reasoning)

    async def _decide(
        self,
        session,
        strategy,
        market,
        price,
        signal,
        indicators,
        sentiment,
        position,
        news_entry: bool = False,
        checks: dict | None = None,
    ) -> tuple[str, float | None, float, str, str, str | None]:
        context = {
            "market": market,
            "price": price,
            "strategy": strategy.model_dump(),
            "signal": signal,
            "news_entry_allowed": news_entry,
            "signal_checks": {k: [c.as_dict() for c in v] for k, v in (checks or {}).items()},
            "soft_filters_failed": [
                c.text for c in (checks or {}).get("buy", []) if c.soft and not c.ok
            ],
            "indicators": indicators,
            "news": sentiment.as_dict(),
            "position": None
            if position is None
            else {
                "amount": position.amount,
                "entry_price": position.entry_price,
                "unrealized_pct": round((price / position.entry_price - 1) * 100, 2),
                # What closing now would really yield after the exit fee, vs. EUR spent.
                "unrealized_net_pct": round(
                    (position.amount * price * (1 - self.fee_pct / 100) / position.entry_quote - 1)
                    * 100,
                    2,
                ),
                "opened_at": position.opened_at,
                "stop_price": position.stop_price,
                "take_profit_price": position.take_profit_price,
            },
            "limits": {"max_trade_eur": self.limits.max_trade_eur},
            "costs": {
                "fee_pct_per_side": self.fee_pct,
                "round_trip_pct": round(2 * self.fee_pct, 4),
                "note": "kopen en weer verkopen kost samen round_trip_pct; een trade moet meer "
                "stijgen om netto iets op te leveren",
            },
            "recent_performance": performance_summary(session, self.mode),
        }
        if self.decision_maker.available:
            try:
                d: TradeDecision = await self.decision_maker.decide(session, context)
                return (
                    d.action,
                    d.confidence,
                    min(d.amount_quote, self.limits.max_trade_eur),
                    d.reasoning_nl,
                    "claude",
                    self.decision_maker.model,
                )
            except BudgetExceeded as exc:
                self.last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Claude-beslissing mislukt: {exc}"
                log.exception("decision failed")
        else:
            self.last_error = "ANTHROPIC_API_KEY ontbreekt"
        # Fallback
        if self.claude_unavailable_mode == "indicators":
            return (
                signal,
                None,
                self.limits.max_trade_eur,
                f"Claude niet beschikbaar ({self.last_error}); strategiesignaal gevolgd",
                "rules",
                None,
            )
        return (
            "hold",
            None,
            0.0,
            f"Claude niet beschikbaar ({self.last_error}); niets gedaan",
            "rules",
            None,
        )

    async def _close(self, session: Session, position: Position, price: float, reason: str):
        amount_quote = position.amount * price
        order = await self.broker.market_order(
            session, position.market, "sell", amount_quote, reason=reason
        )
        fill_quote = order.amount_quote or amount_quote  # net EUR received after fee
        position.fees_eur = (position.fees_eur or 0.0) + (order.fee_quote or 0.0)
        position.status = "closed"
        position.exit_price = order.price or price
        position.exit_quote = fill_quote
        position.closed_at = datetime.now(UTC)
        position.pnl_eur = fill_quote - position.entry_quote
        position.pnl_pct = (fill_quote / position.entry_quote - 1) * 100
        position.close_reason = reason
        position.close_order_id = order.id
        session.commit()
        return order

    async def update_outcomes(self, session: Session, prices: dict[str, float]) -> int:
        """Fill price_1h/24h/72h on past decisions once enough time has passed."""
        now = datetime.now(UTC)
        updated = 0
        rows = session.scalars(
            select(Decision).where(
                Decision.mode == self.mode,
                Decision.created_at >= now - timedelta(hours=80),
                Decision.price_72h.is_(None),
            )
        ).all()
        for d in rows:
            created = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=UTC)
            age = now - created
            price = prices.get(d.market)
            if price is None:
                continue
            changed = False
            if d.price_1h is None and age >= timedelta(hours=1):
                d.price_1h, changed = price, True
            if d.price_24h is None and age >= timedelta(hours=24):
                d.price_24h, changed = price, True
            if d.price_72h is None and age >= timedelta(hours=72):
                d.price_72h, changed = price, True
            updated += 1 if changed else 0
        session.commit()
        return updated

    # -- evaluation --

    async def run_evaluation(self, session: Session, days: int = 7) -> Evaluation:
        strategy = load_strategy(session)
        closed = closed_positions(session, self.mode, days)
        decisions = [d for d in recent_decisions(session, self.mode, 200) if d.source != "rules"]
        context = {
            "period_days": days,
            "mode": self.mode,
            "strategy": strategy.model_dump(),
            "performance": performance_summary(session, self.mode, days),
            "closed_positions": [
                {
                    "market": p.market,
                    "opened_at": p.opened_at,
                    "closed_at": p.closed_at,
                    "entry_price": p.entry_price,
                    "exit_price": p.exit_price,
                    "pnl_pct": round(p.pnl_pct or 0, 2),
                    "reason": p.close_reason,
                }
                for p in closed
            ],
            "decisions": [
                {
                    "when": d.created_at,
                    "market": d.market,
                    "signal": d.signal,
                    "news_label": d.news_label,
                    "news_score": d.news_score,
                    "action": d.action,
                    "executed": d.executed,
                    "blocked_by": d.blocked_by,
                    "reasoning": d.reasoning,
                    "move_1h_pct": round((d.price_1h / d.price - 1) * 100, 2)
                    if d.price_1h
                    else None,
                    "move_24h_pct": round((d.price_24h / d.price - 1) * 100, 2)
                    if d.price_24h
                    else None,
                    "move_72h_pct": round((d.price_72h / d.price - 1) * 100, 2)
                    if d.price_72h
                    else None,
                }
                for d in decisions[:60]
            ],
        }
        result = await self.evaluator.evaluate(session, context)
        proposal_json = None
        backtest_json = None
        if result.proposed_strategy is not None:
            proposal_json = result.proposed_strategy.model_dump_json()
            try:
                candles = await self.client.candles(
                    result.proposed_strategy.market, result.proposed_strategy.interval, 500
                )
                backtest_json = json.dumps(
                    {
                        "proposed": backtest(
                            result.proposed_strategy, candles, 1000, self.backtest_fee_pct
                        ).summary(),
                        "current": backtest(
                            strategy, candles, 1000, self.backtest_fee_pct
                        ).summary()
                        if strategy.market == result.proposed_strategy.market
                        else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                backtest_json = json.dumps({"error": str(exc)})
        report = result.report_nl
        if result.proposal_rationale_nl:
            report += "\n\n**Voorstel:** " + result.proposal_rationale_nl
        ev = Evaluation(
            model=self.evaluator.model,
            period_days=days,
            report_nl=report,
            proposed_strategy=proposal_json,
            proposal_backtest=backtest_json,
        )
        session.add(ev)
        session.commit()
        session.refresh(ev)
        return ev
