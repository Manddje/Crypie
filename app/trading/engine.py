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
from app.news.service import load_market_mood, market_sentiment
from app.strategy.describe import ConditionCheck, explain_signal, levels_summary, signal_reason_nl
from app.strategy.engine import backtest, compute_series, signal_at
from app.strategy.model import LOOKBACK_OPS, Strategy
from app.strategy.store import load_strategy
from app.trading import risk
from app.trading.events import MarketEvent, active_event, recent_shock, upcoming_events
from app.trading.journal import JournalFilter, journal_entries, notes_for_claude
from app.trading.plan import load_plan
from app.trading.risk import RiskLimits
from app.trading.trend import load_trend

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


TRAJECTORY_CANDLES = 6


def recent_trajectory(series: dict[str, list], i: int, n: int = TRAJECTORY_CANDLES) -> dict:
    """The last ``n`` candles of close and every indicator, oldest first, plus the 24h context.

    Claude uses this to judge whether a dip is still falling or has started to turn; a single
    indicator value cannot show that.
    """
    start = max(0, i - n + 1)
    rows = {
        k: [round(v[j], 6) if v[j] is not None else None for j in range(start, i + 1)]
        for k, v in series.items()
        if k not in ("open", "high", "low", "volume")
    }
    close = series["close"]
    day = [c for c in close[max(0, i - 23) : i + 1] if c is not None]
    low_24h = min(day) if day else None
    high_24h = max(day) if day else None
    first = day[0] if day else None
    out: dict = {
        "note": "oudste eerst; laatste waarde = nu",
        "values": rows,
        "close_change_24h_pct": round((close[i] / first - 1) * 100, 2) if first else None,
        "close_above_low_24h_pct": round((close[i] / low_24h - 1) * 100, 2) if low_24h else None,
        "close_below_high_24h_pct": round((1 - close[i] / high_24h) * 100, 2) if high_24h else None,
    }
    # Still falling? Both the price and every oscillator-like series lower than one candle ago.
    if i >= 1 and close[i - 1] is not None:
        out["close_falling"] = close[i] < close[i - 1]
    return out


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
        watch_max_min: int = 180,
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
        self.trend_context = True  # pass the long-term trend (PlanB-style) to Claude
        # Risk-off: no new buys around calendar events and after a news shock.
        self.calendar: list[MarketEvent] = []
        self.risk_off_enabled = True
        self.risk_off_before_min = 60
        self.risk_off_after_min = 120
        self.shock_pause_hours = 6
        self.last_run: datetime | None = None
        self.last_results: list[dict[str, Any]] = []
        self.last_error: str | None = None
        # Dip watch: markets where the dip conditions hold but the turn has not come yet.
        self.watch_max_min = watch_max_min
        self.watch: dict[str, dict[str, Any]] = {}
        self.watch_cooldown: dict[str, datetime] = {}
        self.last_watch_run: datetime | None = None

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

    # -- dip watch --

    async def run_watch(self, session: Session) -> list[CycleResult]:
        """Re-check only the armed markets; called every WATCH_INTERVAL_MIN by the scheduler."""
        if not self.watch:
            return []
        strategy = load_strategy(session)
        markets = [m for m in self.watch if m in self.markets]
        for stale in set(self.watch) - set(markets):
            self.watch.pop(stale, None)
        if not markets:
            return []
        prices = await self.client.ticker_prices(markets)
        results = []
        for market in markets:
            try:
                results.append(await self.run_market(session, strategy, market, prices, watch=True))
            except Exception as exc:  # noqa: BLE001
                log.exception("watch check failed for %s", market)
                results.append(
                    CycleResult(
                        market, prices.get(market, 0.0), "error", "hold", "error", False, str(exc)
                    )
                )
        self.last_watch_run = datetime.now(UTC)
        return results

    async def _price_context(self, market: str, price: float) -> dict[str, Any]:
        """Where the price sits against the last 7 and 30 days: discount is not the bottom."""
        try:
            daily = await self.client.candles(market, "1d", 31)
        except Exception as exc:  # noqa: BLE001 - context only, never block a cycle
            log.warning("daily candles for %s failed: %s", market, exc)
            return {}
        if not daily:
            return {}
        highs = [c[2] for c in daily]
        closes = [c[4] for c in daily]
        avg30 = sum(closes) / len(closes)
        return {
            "below_7d_high_pct": round((1 - price / max(highs[-7:])) * 100, 2),
            "below_30d_high_pct": round((1 - price / max(highs)) * 100, 2),
            "vs_30d_average_pct": round((price / avg30 - 1) * 100, 2),
            "change_7d_pct": round((price / closes[-8] - 1) * 100, 2) if len(closes) >= 8 else None,
            "change_30d_pct": round((price / closes[0] - 1) * 100, 2),
            "note": "korting t.o.v. het recente hoog is context, geen bodem; een dalende "
            "markt kan verder dalen",
        }

    def watch_status(self) -> list[dict[str, Any]]:
        return [
            {"market": m, **{k: v for k, v in w.items()}} for m, w in sorted(self.watch.items())
        ]

    def _arm_state(
        self, strategy: Strategy, checks: dict[str, list[ConditionCheck]]
    ) -> tuple[bool, list[ConditionCheck], list[ConditionCheck]]:
        """(dip holds, dip checks, turn checks) for the buy side.

        The "dip" is every hard, non-lookback buy condition (RSI below 35); the "turn" is the
        lookback conditions (RSI rising). A strategy without a turn condition is never armed.
        """
        hard = [c for c in strategy.buy if not c.soft] or list(strategy.buy)
        pairs = [(cond, chk) for cond, chk in zip(strategy.buy, checks["buy"], strict=True)]
        turn = [chk for cond, chk in pairs if cond.op in LOOKBACK_OPS and cond in hard]
        dip = [chk for cond, chk in pairs if cond.op not in LOOKBACK_OPS and cond in hard]
        return bool(turn) and bool(dip) and all(c.ok for c in dip), dip, turn

    def risk_off(self, session: Session, market: str, now: datetime) -> dict[str, Any] | None:
        """Why no new position may be opened right now, or None: a calendar window or a shock."""
        if not self.risk_off_enabled:
            return None
        event = active_event(self.calendar, now, self.risk_off_before_min, self.risk_off_after_min)
        if event is not None:
            start, end = event.window(self.risk_off_before_min, self.risk_off_after_min)
            return {
                "kind": "event",
                "label": event.label,
                "at": event.at.isoformat(),
                "until": end.isoformat(),
                "reason_nl": (
                    f"{event.label} om {event.at:%H:%M} UTC; geen nieuwe aankopen van "
                    f"{start:%H:%M} tot {end:%H:%M} UTC"
                ),
            }
        shock = recent_shock(session, market.split("-")[0], self.shock_pause_hours, now)
        if shock is not None:
            published = shock.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            until = published + timedelta(hours=self.shock_pause_hours)
            return {
                "kind": "shock",
                "label": shock.summary_nl or shock.title,
                "at": published.isoformat(),
                "until": until.isoformat(),
                "reason_nl": (
                    f"nieuwsschok ({shock.summary_nl or shock.title}); geen nieuwe aankopen tot "
                    f"{until:%H:%M} UTC"
                ),
            }
        return None

    def events_context(self, session: Session, market: str, now: datetime) -> dict[str, Any]:
        """What Claude and the UI see: the active risk-off (if any) and the next events."""
        return {
            "enabled": self.risk_off_enabled,
            "risk_off": self.risk_off(session, market, now),
            "upcoming": [
                e.as_dict() for e in upcoming_events(self.calendar, now, 3, horizon_hours=72)
            ],
            "window_min": {"before": self.risk_off_before_min, "after": self.risk_off_after_min},
            "shock_pause_hours": self.shock_pause_hours,
        }

    def events_status(self, session: Session) -> dict[str, Any]:
        now = datetime.now(UTC)
        shocks = {}
        for market in self.markets:
            ro = self.risk_off(session, market, now)
            if ro and ro["kind"] == "shock":
                shocks[market] = ro
        event = (
            active_event(self.calendar, now, self.risk_off_before_min, self.risk_off_after_min)
            if self.risk_off_enabled
            else None
        )
        return {
            "enabled": self.risk_off_enabled,
            "active_event": None
            if event is None
            else {
                **event.as_dict(),
                "until": event.window(0, self.risk_off_after_min)[1].isoformat(),
            },
            "shocks": shocks,
            "upcoming": [e.as_dict() for e in upcoming_events(self.calendar, now, 4)],
            "before_min": self.risk_off_before_min,
            "after_min": self.risk_off_after_min,
            "shock_pause_hours": self.shock_pause_hours,
        }

    async def run_market(
        self,
        session: Session,
        strategy: Strategy,
        market: str,
        prices: dict[str, float],
        watch: bool = False,
    ) -> CycleResult:
        candles = await self.client.candles(market, strategy.interval, strategy.candles_needed())
        series = compute_series(strategy, candles)
        i = len(candles) - 1
        position = open_position(session, self.mode, market)
        # With an open position the sell side goes first: a buy signal must not hide it.
        signal = signal_at(strategy, series, i, has_position=position is not None)
        checks = explain_signal(strategy, series, i)
        signal_reason = signal_reason_nl(signal, checks)
        price = prices.get(market) or candles[-1][4]
        now = datetime.now(UTC)
        risk_off = self.risk_off(session, market, now) if position is None else None

        # Dip watch: arm on the dip, fire on the turn, give up after watch_max_min.
        dip_holds, dip_checks, turn_checks = self._arm_state(strategy, checks)
        armed = self.watch.get(market)
        watch_triggered = False
        if position is not None or risk_off is not None:
            self.watch.pop(market, None)  # nothing to buy (yet): no dip to watch either
        elif armed is not None:
            if signal == "buy" or all(c.ok for c in turn_checks):
                # The turn came (the full signal, or the turn condition alone on the dip we
                # armed on): straight to Claude.
                watch_triggered = True
                signal = "buy"
                signal_reason = (
                    "Dipbewaking: dip gezien om "
                    + armed["since"].strftime("%H:%M")
                    + " UTC en nu de draai: "
                    + "; ".join(f"{c.text} ✓ ({c.values_nl})" for c in turn_checks)
                    + ". "
                    + signal_reason_nl("buy", checks)
                )
                self.watch.pop(market, None)
            elif now - armed["since"] > timedelta(minutes=self.watch_max_min):
                self.watch.pop(market, None)
                log.info("dip watch %s: no turn in %s min, giving up", market, self.watch_max_min)
            else:
                armed["last_check"] = now
                armed["values"] = {c.text: c.left for c in dip_checks + turn_checks}
        elif signal == "buy":
            pass  # a plain signal; the normal path below handles it
        elif dip_holds and now >= self.watch_cooldown.get(market, now):
            self.watch[market] = {
                "since": now,
                "last_check": now,
                "dip": "; ".join(f"{c.text} ({c.values_nl})" for c in dip_checks),
                "waiting_for": "; ".join(c.text for c in turn_checks),
                "values": {c.text: c.left for c in dip_checks + turn_checks},
            }
            log.info("dip watch %s armed: %s", market, self.watch[market]["dip"])
        if watch and not watch_triggered and signal != "buy":
            # A quiet watch check: no journal entry, the hourly cycle writes those.
            return CycleResult(
                market, price, signal, "hold", "watch", False, "dipbewaking: nog geen draai"
            )
        if watch_triggered:
            # Claude decides once per dip; do not re-arm on the same dip for a while.
            self.watch_cooldown[market] = now + timedelta(minutes=60)
        indicators = {
            k: round(v[i], 6)
            for k, v in series.items()
            if k not in ("open", "high", "low", "volume") and v[i] is not None
        }
        trajectory = recent_trajectory(series, i)
        trajectory["price_context"] = await self._price_context(market, price)
        coin = market.split("-")[0]
        sentiment = market_sentiment(session, coin, 48)

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

        # 1. Mechanical exits first: stop-loss / trailing stop / take-profit on the live price.
        if position is not None:
            reason = None
            trailed = self._trail_stop(session, position, strategy, price)
            if position.stop_price and price <= position.stop_price:
                reason = "trailing_stop" if trailed else "stop_loss"
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
        if risk_off is not None and position is None and (needs_claude or signal == "buy"):
            # 2b. Risk-off: a calendar window or a news shock. No new position, whatever the
            # signal or the news says; Claude is not asked.
            decision.reasoning = (
                f"{signal_reason} Risk-off: {risk_off['reason_nl']}. "
                "Geen nieuwe positie; verkopen en stop-loss blijven mogelijk."
            )
            decision.blocked_by = "risk-off"
            session.add(decision)
            session.commit()
            return CycleResult(market, price, signal, "hold", "rules", False, decision.reasoning)
        if not needs_claude:
            news = (
                "geen beoordeeld nieuws"
                if sentiment.items == 0
                else f"nieuws {sentiment.label()} ({sentiment.score:+.2f}, "
                f"{sentiment.items} nieuwsfeiten uit {sentiment.articles} berichten, "
                f"{sentiment.high_impact} van hoge impact)"
            )
            if position is not None:
                why = "positie blijft open: geen verkoopsignaal en geen zwaar negatief nieuws"
            elif signal == "sell":
                why = "verkoopsignaal zonder open positie, niets te verkopen"
            elif market in self.watch:
                why = (
                    "dip gesignaleerd, dipbewaking actief: wacht op de draai en kijkt daarvoor "
                    "vaker dan de gewone cyclus"
                )
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
            trajectory,
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
            elif (ro := self.risk_off(session, market, datetime.now(UTC))) is not None:
                blocked = f"risk-off: {ro['reason_nl']}"
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
                                stop_price=strategy.initial_stop(fill),
                                high_price=fill,
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
        trajectory: dict | None = None,
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
            "support_levels": levels_summary(strategy, indicators),
            "recent_candles": trajectory or {},
            "news": sentiment.as_dict(),
            "market_mood": load_market_mood(session),  # Fear & Greed index, market-wide
            "long_term_trend": load_trend(session, market) if self.trend_context else None,
            "market_events": self.events_context(session, market, datetime.now(UTC)),
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
            # The owner's own plan (goal, rules, lessons) from /plan; None when not written.
            "trading_plan": load_plan(session).as_context(),
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

    @staticmethod
    def _trail_stop(session: Session, position: Position, strategy, price: float) -> bool:
        """Raise the stop with the highest price since entry. True when the stop was trailed up.

        The stop only moves up, never down, and never below the stop-loss set at entry. A
        strategy without trailing_stop_pct leaves the stop where it is.
        """
        if not strategy.trailing_stop_pct:
            return False
        high = max(position.high_price or position.entry_price, price)
        trail = strategy.trailing_stop(high, position.entry_price)
        changed = False
        if high != position.high_price:
            position.high_price, changed = high, True
        if trail is not None and trail > (position.stop_price or 0):
            position.stop_price, changed = trail, True
        if changed:
            session.commit()
        floor = strategy.initial_stop(position.entry_price) or 0
        return bool(position.stop_price and position.stop_price > floor + 1e-12)

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
            "trading_plan": load_plan(session).as_context(),
            # What the owner wrote in the journal about recent trades.
            "owner_notes": notes_for_claude(
                journal_entries(session, self.mode, JournalFilter(days=max(days, 30)))
            ),
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
