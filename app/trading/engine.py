"""The trading loop: one cycle per market combines strategy, news, Claude and risk limits.

Concurrency: every run that can trade a market (the scheduled cycle, the dip watch, the
manual cycle button, manual orders and the exit loop) holds that market's lock, and takes one
snapshot of the broker (and so of the mode) when it starts. Across processes the database
allows one active position per mode and market (see ``Position``), so two app instances
during a rolling deploy cannot both buy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.decision_maker import DecisionMaker, TradeDecision
from app.ai.evaluator import Evaluator
from app.bitvavo.broker import Broker, OrderUncertain
from app.bitvavo.client import BitvavoClient, BitvavoError
from app.db import Decision, Evaluation, NewsItem, Order, Position, Setting
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
# News alone only counts when the news fact is reported by this many different outlets.
NEWS_MIN_SOURCES = 2
NEWS_HIGH_IMPACT = 2

# Bitvavo's minimum order value; a remainder below it can never be sold on its own.
EXCHANGE_MIN_ORDER_EUR = 5.0
# Live candles: enough for the longest indicator plus its warm-up, within Bitvavo's maximum.
LIVE_CANDLES_MIN = 300
LIVE_CANDLES_MAX = 1440
# Deadlines, so one hanging HTTP or Claude call cannot hold up the other markets.
DATA_TIMEOUT_S = 60.0
DECISION_TIMEOUT_S = 150.0
MARKET_RUN_TIMEOUT_S = 300.0
EXIT_LOCK_WAIT_S = 30.0
# After an order is refused, no new Claude consult in that market for this long.
ORDER_BACKOFF_MIN = 60
# An "opening"/"closing" claim without a live order behind it is released after this long.
CLAIM_STALE = timedelta(minutes=10)
ENGINE_STATE_KEY = "engine_state"


class ManualOrderError(ValueError):
    """A manual order that the rules do not allow; the message is Dutch, for the owner."""


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


# -- news confirmation -------------------------------------------------------------------

# Aggregators name themselves, not the outlet; for those the article's host is the outlet.
_AGGREGATOR_NAMES = {"google news", "gdelt", "cryptopanic", "cryptocurrency.cv", ""}


def _outlet(item: NewsItem) -> str:
    name = (item.source_name or "").strip().lower()
    if name not in _AGGREGATOR_NAMES:
        return name
    host = (urlparse(item.url or "").hostname or "").lower()
    return host.removeprefix("www.") or name


def _event_key(item: NewsItem) -> str:
    if item.event_key:
        return item.event_key
    return "title:" + " ".join((item.title or "").lower().split())


def news_sources(
    session: Session,
    coin: str,
    positive: bool,
    hours: int = 48,
    now: datetime | None = None,
) -> int:
    """The most distinct outlets behind one high-impact news fact pointing one way for ``coin``.

    ``positive`` selects facts with an average sentiment of at least NEWS_ENTRY_MIN_SCORE,
    otherwise at most NEWS_EXIT_MAX_SCORE. A single outlet (or a few articles from one site)
    gives 1, which is not enough to trade on news alone.
    """
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(NewsItem).where(
            NewsItem.assessed.is_(True),
            NewsItem.relevant.is_(True),
            NewsItem.published_at >= now - timedelta(hours=hours),
        )
    ).all()
    events: dict[str, list[NewsItem]] = {}
    for it in rows:
        coins = it.coin_list()
        if coin not in coins and "ALL" not in coins:
            continue
        if it.sentiment is None or not it.impact:
            continue
        events.setdefault(_event_key(it), []).append(it)
    best = 0
    for members in events.values():
        if max(m.impact or 0 for m in members) < NEWS_HIGH_IMPACT:
            continue
        sentiment = sum(m.sentiment or 0.0 for m in members) / len(members)
        if positive and sentiment < NEWS_ENTRY_MIN_SCORE:
            continue
        if not positive and sentiment > NEWS_EXIT_MAX_SCORE:
            continue
        best = max(best, len({_outlet(m) for m in members}))
    return best


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


def live_candle_limit(strategy: Strategy) -> int:
    """How many candles a live cycle fetches: the longest indicator period plus warm-up.

    EMA-type indicators (and the RSI) need history before their value settles; twice the
    longest period is a safe margin. Never fewer than before (300), never more than Bitvavo
    serves in one call (1440).
    """
    longest = 1
    for ind in strategy.indicators:
        if ind.type == "macd":
            longest = max(longest, ind.slow + ind.signal)
        else:
            longest = max(longest, getattr(ind, "period", 1))
    lookback = max(
        (int(c.right) for c in strategy.buy + strategy.sell if c.op in LOOKBACK_OPS), default=0
    )
    return min(LIVE_CANDLES_MAX, max(LIVE_CANDLES_MIN, 2 * longest + lookback + 2))


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


def _iso(value: datetime) -> str:
    return value.isoformat()


def _from_iso(value: Any) -> datetime | None:
    try:
        out = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=UTC)


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
        # Live only: EUR the bot manages (0 = the whole account), see portfolio().
        self.live_capital_eur = 0.0
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
        # After a refused order: no new Claude consult in that market until this moment.
        self.order_backoff: dict[str, datetime] = {}
        # Exit loop: a broker for positions of the other mode (set by the app), and the open
        # positions it could not protect on its last run.
        self.broker_for_mode: Callable[[str], Broker | None] | None = None
        self.unprotected: list[dict[str, Any]] = []
        self.last_exit_run: datetime | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self.market_timeout_s = MARKET_RUN_TIMEOUT_S
        self.data_timeout_s = DATA_TIMEOUT_S
        self.decision_timeout_s = DECISION_TIMEOUT_S
        self.exit_lock_wait_s = EXIT_LOCK_WAIT_S

    @property
    def mode(self) -> str:
        return self.broker.mode

    def market_lock(self, market: str) -> asyncio.Lock:
        lock = self._locks.get(market)
        if lock is None:
            lock = self._locks[market] = asyncio.Lock()
        return lock

    def broker_for(self, mode: str) -> Broker | None:
        """The broker that may trade positions of ``mode``: the active one, or the one the app
        supplies for the other mode (None when that mode cannot trade, e.g. live without
        LIVE_TRADING_ACK)."""
        if self.broker.mode == mode:
            return self.broker
        if self.broker_for_mode is not None:
            return self.broker_for_mode(mode)
        return None

    # -- state that survives a restart --

    def save_state(self, session: Session) -> None:
        """Persist the dip watch, its cooldown and the order backoff (best-effort)."""
        state = {
            "watch": {
                m: {
                    **{k: v for k, v in w.items() if k not in ("since", "last_check")},
                    "since": _iso(w["since"]),
                    "last_check": _iso(w.get("last_check") or w["since"]),
                }
                for m, w in self.watch.items()
            },
            "watch_cooldown": {m: _iso(t) for m, t in self.watch_cooldown.items()},
            "order_backoff": {m: _iso(t) for m, t in self.order_backoff.items()},
        }
        try:
            row = session.get(Setting, ENGINE_STATE_KEY)
            payload = json.dumps(state, ensure_ascii=False)
            if row is None:
                session.add(Setting(key=ENGINE_STATE_KEY, value=payload))
            else:
                row.value = payload
            session.commit()
        except Exception:  # noqa: BLE001 - state is a convenience, never fail a cycle on it
            session.rollback()
            log.exception("could not save the engine state")

    def load_state(self, session: Session) -> None:
        """Restore what ``save_state`` stored; anything unreadable is dropped with a warning."""
        try:
            row = session.get(Setting, ENGINE_STATE_KEY)
            state = json.loads(row.value) if row else {}
            now = datetime.now(UTC)
            watch = {}
            for market, w in (state.get("watch") or {}).items():
                since = _from_iso(w.get("since"))
                if since is None or now - since > timedelta(minutes=self.watch_max_min):
                    continue
                watch[market] = {**w, "since": since, "last_check": _from_iso(w.get("last_check"))}
            cooldown = {
                m: t
                for m, v in (state.get("watch_cooldown") or {}).items()
                if (t := _from_iso(v)) is not None and t > now
            }
            backoff = {
                m: t
                for m, v in (state.get("order_backoff") or {}).items()
                if (t := _from_iso(v)) is not None and t > now
            }
        except Exception:  # noqa: BLE001
            log.exception("ignoring unreadable engine state")
            return
        self.watch, self.watch_cooldown, self.order_backoff = watch, cooldown, backoff

    # -- portfolio helpers --

    async def portfolio(
        self, session: Session, prices: dict[str, float], broker: Broker | None = None
    ) -> tuple[float, float]:
        """Return (total_value_eur, exposure_eur) against which the limits are checked.

        Live with LIVE_CAPITAL_EUR set: that capital, and only the bot's own positions as
        exposure. Otherwise the broker balances (the whole account in live mode).
        """
        broker = broker or self.broker
        if broker.mode == "live" and self.live_capital_eur > 0:
            return self.live_capital_eur, risk.position_exposure(session, "live", prices)
        balances = await broker.balances(session)
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

    async def _guarded_market(
        self,
        session: Session,
        strategy: Strategy,
        market: str,
        prices: dict[str, float],
        watch: bool = False,
    ) -> CycleResult:
        try:
            return await asyncio.wait_for(
                self.run_market(session, strategy, market, prices, watch=watch),
                self.market_timeout_s,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"{market}: afgebroken, geen resultaat binnen de tijdslimiet"
            ) from exc

    async def run_cycle(self, session: Session) -> list[CycleResult]:
        strategy = load_strategy(session)
        prices = await self.client.ticker_prices(self.markets)
        results: list[CycleResult] = []
        for market in list(self.markets):
            try:
                results.append(await self._guarded_market(session, strategy, market, prices))
            except Exception as exc:  # one market must not stop the others
                session.rollback()
                log.exception("cycle failed for %s", market)
                results.append(
                    CycleResult(
                        market, prices.get(market, 0.0), "error", "hold", "error", False, str(exc)
                    )
                )
        try:
            await self.update_outcomes(session, prices)
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("updating decision outcomes failed")
        self.last_run = datetime.now(UTC)
        self.last_results = [r.as_dict() for r in results]
        self.save_state(session)
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
                results.append(
                    await self._guarded_market(session, strategy, market, prices, watch=True)
                )
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                log.exception("watch check failed for %s", market)
                results.append(
                    CycleResult(
                        market, prices.get(market, 0.0), "error", "hold", "error", False, str(exc)
                    )
                )
        self.last_watch_run = datetime.now(UTC)
        self.save_state(session)
        return results

    async def _price_context(self, market: str, price: float) -> dict[str, Any]:
        """Where the price sits against the last 7 and 30 days: discount is not the bottom."""
        try:
            daily = await asyncio.wait_for(
                self.client.candles(market, "1d", 31), self.data_timeout_s
            )
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
        async with self.market_lock(market):
            # One snapshot for the whole run: a mode switch halfway can never make this run
            # trade through the other mode's broker.
            broker = self.broker
            return await self._run_market(session, broker, strategy, market, prices, watch)

    async def _run_market(
        self,
        session: Session,
        broker: Broker,
        strategy: Strategy,
        market: str,
        prices: dict[str, float],
        watch: bool,
    ) -> CycleResult:
        mode = broker.mode
        candles = await asyncio.wait_for(
            self.client.candles(market, strategy.interval, live_candle_limit(strategy)),
            self.data_timeout_s,
        )
        series = compute_series(strategy, candles)
        i = len(candles) - 1
        position = open_position(session, mode, market)
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
            mode=mode,
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
            result = await self._mechanical_exit(
                session, broker, strategy, position, price, decision
            )
            if result is not None:
                return result

        # 2. Nothing to decide? Save the Claude call.
        news_entry_hint = (
            position is None
            and signal == "hold"
            and sentiment.high_impact > 0
            and sentiment.score >= NEWS_ENTRY_MIN_SCORE
        )
        news_exit_hint = (
            position is not None
            and sentiment.high_impact > 0
            and sentiment.score <= NEWS_EXIT_MAX_SCORE
        )
        confirmed = True
        if news_entry_hint or news_exit_hint:
            # News alone must be confirmed by a second outlet before it may move money.
            confirmed = news_sources(session, coin, positive=news_entry_hint) >= NEWS_MIN_SOURCES
        news_entry = news_entry_hint and confirmed
        news_exit = news_exit_hint and confirmed
        needs_claude = (
            signal == "buy"
            and position is None
            or signal == "sell"
            and position is not None
            or news_exit
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
        if needs_claude:
            # 2c. An order in this market is still unsettled, or one was refused recently:
            # do not ask Claude the same question again.
            hold_reason = None
            until = self.order_backoff.get(market)
            if risk.has_unsettled_order(session, mode, market):
                hold_reason, decision.blocked_by = (
                    "Er staat nog een order in deze markt waarvan de uitkomst niet bekend is; "
                    "de bot wacht tot die is gecontroleerd.",
                    "order onderweg",
                )
            elif until is not None and now < until:
                hold_reason, decision.blocked_by = (
                    f"Na een geweigerde order wacht de bot tot {until:%H:%M} UTC met een "
                    "nieuwe poging; Claude wordt zolang niet gevraagd.",
                    "wacht na geweigerde order",
                )
            if hold_reason:
                decision.reasoning = f"{signal_reason} {hold_reason}"
                session.add(decision)
                session.commit()
                return CycleResult(
                    market, price, signal, "hold", "rules", False, decision.reasoning
                )
        if not needs_claude:
            news = (
                "geen beoordeeld nieuws"
                if sentiment.items == 0
                else f"nieuws {sentiment.label()} ({sentiment.score:+.2f}, "
                f"{sentiment.items} nieuwsfeiten uit {sentiment.articles} berichten, "
                f"{sentiment.high_impact} van hoge impact)"
            )
            if (news_entry_hint or news_exit_hint) and not confirmed:
                why = (
                    "het nieuws is niet bevestigd door een tweede bron; op één bericht "
                    + ("verkoopt" if news_exit_hint else "koopt")
                    + " de bot niet"
                )
            elif position is not None:
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
            mode=mode,
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
        try:
            if action == "buy":
                if position is not None:
                    blocked = "kopen met een open positie is niet toegestaan"
                elif signal != "buy" and not news_entry:
                    blocked = (
                        "kopen zonder buy-signaal of bevestigd sterk positief nieuws is niet "
                        "toegestaan"
                    )
                elif (ro := self.risk_off(session, market, datetime.now(UTC))) is not None:
                    blocked = f"risk-off: {ro['reason_nl']}"
                else:
                    total, exposure = await self.portfolio(session, prices, broker)
                    allowed, blocked = risk.check_buy(
                        session,
                        self.limits,
                        mode,
                        market,
                        amount_quote,
                        total,
                        exposure,
                        unrealized=risk.unrealized_pnl(session, mode, prices),
                    )
                    if blocked is None:
                        order, opened, blocked = await self._buy(
                            session,
                            broker,
                            strategy,
                            market,
                            allowed,
                            price,
                            reason=f"{source}: {reasoning[:120]}",
                        )
                        if order is not None:
                            order_id = order.id
                        if opened is not None:
                            decision.amount_quote = opened.entry_quote
                            executed = True
            elif action == "sell":
                if position is None:
                    blocked = "geen open positie om te verkopen"
                elif signal != "sell" and not news_exit:
                    blocked = (
                        "verkopen zonder verkoopsignaal of bevestigd zwaar negatief nieuws is "
                        "niet toegestaan"
                    )
                elif risk.is_paused(session) and signal != "sell":
                    blocked = "handelen is gepauzeerd (kill switch)"
                else:
                    order, blocked, note = await self._sell(
                        session,
                        broker,
                        position,
                        price,
                        "signal" if signal == "sell" else "news_exit",
                    )
                    if order is not None:
                        order_id = order.id
                    if blocked is None:
                        executed = True
                        if note:
                            decision.reasoning = f"{decision.reasoning} ({note})"
        except OrderUncertain as exc:
            blocked = str(exc)
            order_id = exc.order.id
        except (ValueError, BitvavoError) as exc:
            # Refused by the exchange or the paper wallet: journal it and back off, so the
            # next cycle does not pay Claude for the same refused order.
            session.rollback()
            blocked = f"order geweigerd: {exc}"
            self.order_backoff[market] = datetime.now(UTC) + timedelta(minutes=ORDER_BACKOFF_MIN)
            log.warning("%s order in %s refused: %s", action, market, exc)
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
        mode: str | None = None,
    ) -> tuple[str, float | None, float, str, str, str | None]:
        mode = mode or self.mode
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
            "recent_performance": performance_summary(session, mode),
            # The owner's own plan (goal, rules, lessons) from /plan; None when not written.
            "trading_plan": load_plan(session).as_context(),
        }
        if self.decision_maker.available:
            try:
                d: TradeDecision = await asyncio.wait_for(
                    self.decision_maker.decide(session, context), self.decision_timeout_s
                )
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
            except TimeoutError:
                self.last_error = (
                    f"Claude-beslissing duurde langer dan {self.decision_timeout_s:.0f} s"
                )
                log.warning("decision for %s timed out", market)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Claude-beslissing mislukt: {exc}"
                log.exception("decision failed")
        else:
            self.last_error = "ANTHROPIC_API_KEY ontbreekt"
        return self._fallback(signal, position, sentiment)

    def _fallback(
        self, signal: str, position, sentiment
    ) -> tuple[str, float | None, float, str, str, str | None]:
        """What to do without Claude. A strategy sell signal on an open position is always
        followed (in both modes); ``indicators`` also follows a buy signal, but with half the
        budget and not against strongly negative news. Risk-off is enforced afterwards."""
        why = f"Claude niet beschikbaar ({self.last_error})"
        if signal == "sell" and position is not None:
            return (
                "sell",
                None,
                0.0,
                f"{why}; verkoopsignaal van de strategie gevolgd",
                "rules",
                None,
            )
        if self.claude_unavailable_mode == "indicators" and signal == "buy" and position is None:
            if sentiment.high_impact > 0 and sentiment.score <= NEWS_EXIT_MAX_SCORE:
                return (
                    "hold",
                    None,
                    0.0,
                    f"{why}; koopsignaal genegeerd wegens zwaar negatief nieuws",
                    "rules",
                    None,
                )
            return (
                "buy",
                None,
                self.limits.max_trade_eur * NEWS_ENTRY_SIZE_FACTOR,
                f"{why}; strategiesignaal gevolgd met de halve inzet",
                "rules",
                None,
            )
        return ("hold", None, 0.0, f"{why}; niets gedaan", "rules", None)

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

    # -- execution --

    async def _mechanical_exit(
        self,
        session: Session,
        broker: Broker,
        strategy: Strategy,
        position: Position,
        price: float,
        decision: Decision | None = None,
    ) -> CycleResult | None:
        """Stop-loss, trailing stop or take-profit at ``price``; None when none is hit."""
        reason = None
        trailed = self._trail_stop(session, position, strategy, price)
        if position.stop_price and price <= position.stop_price:
            reason = "trailing_stop" if trailed else "stop_loss"
        elif position.take_profit_price and price >= position.take_profit_price:
            reason = "take_profit"
        if not reason:
            return None
        if decision is None:
            decision = Decision(
                mode=position.mode,
                market=position.market,
                price=price,
                signal="hold",
                has_position=True,
                source="mechanical",
                action="sell",
            )
        decision.source, decision.action = "mechanical", "sell"
        decision.reasoning = f"{reason} geraakt op {price:.2f}"
        market, signal = position.market, decision.signal
        try:
            order, blocked, note = await self._sell(session, broker, position, price, reason)
        except OrderUncertain as exc:
            order, blocked, note = exc.order, str(exc), None
        except (ValueError, BitvavoError) as exc:
            session.rollback()
            order, blocked, note = None, f"verkoop mislukt: {exc}", None
            log.error("%s for %s %s failed: %s", reason, position.mode, market, exc)
        if note:
            decision.reasoning += f" ({note})"
        decision.executed = blocked is None
        decision.blocked_by = blocked
        decision.order_id = order.id if order is not None else None
        session.add(decision)
        session.commit()
        return CycleResult(market, price, signal, "sell", "mechanical", blocked is None, reason)

    async def _buy(
        self,
        session: Session,
        broker: Broker,
        strategy: Strategy,
        market: str,
        amount_eur: float,
        price: float,
        reason: str,
    ) -> tuple[Order | None, Position | None, str | None]:
        """Claim the market, buy for ``amount_eur`` and open a position with the strategy's
        stops. Returns (order, position, blocked); the caller commits."""
        claim = Position(
            mode=broker.mode,
            market=market,
            status="opening",
            amount=0.0,
            entry_price=price,
            entry_quote=0.0,
        )
        session.add(claim)
        try:
            session.commit()  # the database refuses a second active position in this market
        except IntegrityError:
            session.rollback()
            return None, None, f"er loopt al een positie of order in {market}"
        try:
            order = await broker.market_order(
                session, market, "buy", amount_eur, reason=reason, position_id=claim.id
            )
        except OrderUncertain:
            raise  # the claim stays until reconcile() knows what happened
        except Exception:
            session.rollback()
            session.delete(claim)
            session.commit()
            raise
        if not self._book_buy(session, claim, order, strategy, price):
            return order, None, f"kooporder niet gevuld (status {order.status})"
        return order, claim, None

    @staticmethod
    def _book_buy(
        session: Session, position: Position, order: Order, strategy: Strategy, price: float
    ) -> bool:
        """Turn an ``opening`` claim into an open position with what really filled."""
        coins = order.amount or 0.0
        if coins <= 0:
            log.warning(
                "buy %s in %s did not fill (status %s)", order.id, order.market, order.status
            )
            session.delete(position)
            return False
        fill = order.price or price
        position.status = "open"
        position.amount = coins
        position.entry_price = fill
        position.entry_quote = order.amount_quote or coins * fill  # EUR gone incl. fee
        position.fees_eur = order.fee_quote or 0.0
        position.stop_price = strategy.initial_stop(fill)
        position.high_price = fill
        position.take_profit_price = (
            fill * (1 + strategy.take_profit_pct / 100) if strategy.take_profit_pct else None
        )
        position.open_order_id = order.id
        position.opened_at = datetime.now(UTC)
        if order.status != "filled":
            log.warning(
                "buy %s in %s only partly filled (%s): position of %.8f opened",
                order.id,
                order.market,
                order.status,
                coins,
            )
        return True

    async def _sell(
        self,
        session: Session,
        broker: Broker,
        position: Position,
        price: float,
        reason: str,
        amount: float | None = None,
    ) -> tuple[Order | None, str | None, str | None]:
        """Sell the position (or ``amount`` coins of it) in base currency.

        Returns (order, blocked, note); the caller commits. A partial fill books the sold part
        and keeps the rest open, with its stops.
        """
        if position.mode != broker.mode:
            raise RuntimeError(
                f"positie {position.id} is {position.mode}, broker is {broker.mode}; niet verkocht"
            )
        full_exit = amount is None or amount >= position.amount * (1 - 1e-9)
        want = position.amount if full_exit else amount
        qty = await broker.sellable(session, position.market, want)
        if qty <= 0:
            return None, f"geen {position.market.split('-')[0]} beschikbaar om te verkopen", None
        claimed = session.execute(
            update(Position)
            .where(Position.id == position.id, Position.status == "open")
            .values(status="closing", closed_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            return None, "positie is al gesloten of wordt al verkocht", None
        session.commit()
        session.refresh(position)
        try:
            order = await broker.market_order(
                session, position.market, "sell", reason=reason, amount=qty, position_id=position.id
            )
        except OrderUncertain:
            raise  # stays "closing" until reconcile() knows what happened
        except Exception:
            session.rollback()
            position.status, position.closed_at = "open", None
            session.commit()
            raise
        sold, closed = self._book_sell(session, position, order, price, reason, qty, full_exit)
        if sold <= 0:
            return order, f"verkooporder niet gevuld (status {order.status})", None
        if not closed:
            note = (
                f"gedeeltelijk gevuld: {sold:.8f} verkocht, {position.amount:.8f} blijft open "
                "met dezelfde stop"
            )
            return order, None, note
        return order, None, None

    @staticmethod
    def _book_sell(
        session: Session,
        position: Position,
        order: Order,
        price: float,
        reason: str,
        requested: float | None = None,
        full_exit: bool = False,
    ) -> tuple[float, bool]:
        """Book what a sell really filled. Returns (coins sold, position fully closed).

        Nothing filled: the position is open again. Part filled: the sold part becomes its
        own closed position and the rest stays open (still protected by its stops).
        """
        sold = order.amount or 0.0
        now = datetime.now(UTC)
        if sold <= 0:
            log.warning(
                "sell %s in %s did not fill (status %s); position stays open",
                order.id,
                order.market,
                order.status,
            )
            position.status, position.closed_at = "open", None
            return 0.0, False
        fill = order.price or price
        remaining = position.amount - sold
        all_requested = requested is not None and sold >= requested * (1 - 1e-6)
        if remaining * fill < EXCHANGE_MIN_ORDER_EUR or (full_exit and all_requested):
            if remaining > 1e-12:
                log.warning(
                    "position %s: %.8f %s left (dust or not on the account); closed in full",
                    position.id,
                    remaining,
                    position.market,
                )
            exit_quote = order.amount_quote or 0.0
            position.status = "closed"
            position.fees_eur = (position.fees_eur or 0.0) + (order.fee_quote or 0.0)
            position.exit_price = fill
            position.exit_quote = exit_quote
            position.closed_at = now
            position.pnl_eur = exit_quote - position.entry_quote
            position.pnl_pct = (
                (exit_quote / position.entry_quote - 1) * 100 if position.entry_quote else 0.0
            )
            position.close_reason = reason
            position.close_order_id = order.id
            return sold, True
        fraction = sold / position.amount
        entry_part = position.entry_quote * fraction
        exit_quote = order.amount_quote or 0.0
        part = Position(
            mode=position.mode,
            market=position.market,
            status="closed",
            amount=sold,
            entry_price=position.entry_price,
            entry_quote=entry_part,
            opened_at=position.opened_at,
            stop_price=position.stop_price,
            high_price=position.high_price,
            take_profit_price=position.take_profit_price,
            exit_price=fill,
            exit_quote=exit_quote,
            closed_at=now,
            pnl_eur=exit_quote - entry_part,
            pnl_pct=(exit_quote / entry_part - 1) * 100 if entry_part else 0.0,
            close_reason=reason,
            open_order_id=position.open_order_id,
            close_order_id=order.id,
            fees_eur=(position.fees_eur or 0.0) * fraction + (order.fee_quote or 0.0),
        )
        session.add(part)
        position.amount = remaining
        position.entry_quote = position.entry_quote - entry_part
        position.fees_eur = (position.fees_eur or 0.0) * (1 - fraction)
        position.status, position.closed_at = "open", None
        log.warning(
            "sell %s in %s partly filled (%s): %.8f sold, %.8f stays open",
            order.id,
            order.market,
            order.status,
            sold,
            remaining,
        )
        return sold, False

    # -- exit loop --

    async def run_exits(self, session: Session) -> list[CycleResult]:
        """Stop-loss, trailing stop and take-profit for every open position, any market and
        any mode, on the current ticker price.

        Runs on its own schedule, independent of MARKETS, candles, Claude, TRADING_ENABLED and
        the kill switch (which only blocks new buys). Each position is sold through the broker
        of its own mode; when that mode cannot trade (live without the environment's consent)
        the position is reported in ``unprotected`` and logged as an error every run.
        """
        positions = list(session.scalars(select(Position).where(Position.status == "open")))
        self.last_exit_run = datetime.now(UTC)
        if not positions:
            self.unprotected = []
            return []
        strategy = load_strategy(session)
        prices = await self.client.ticker_prices(sorted({p.market for p in positions}))
        results: list[CycleResult] = []
        unprotected: list[dict[str, Any]] = []
        for p in positions:
            pid, mode, market = p.id, p.mode, p.market
            if self.broker_for(mode) is None:
                unprotected.append(
                    {
                        "id": pid,
                        "mode": mode,
                        "market": market,
                        "amount": p.amount,
                        "stop_price": p.stop_price,
                        "price": prices.get(market),
                    }
                )
                continue
            price = prices.get(market)
            if not price:
                log.warning("exit check: no price for %s, position %s not checked", market, pid)
                continue
            lock = self.market_lock(market)
            try:
                await asyncio.wait_for(lock.acquire(), self.exit_lock_wait_s)
            except TimeoutError:
                log.warning("exit check for %s skipped: the market is busy", market)
                continue
            try:
                session.refresh(p)
                broker = self.broker_for(mode)  # again: the mode may have switched meanwhile
                if p.status != "open" or broker is None:
                    continue
                result = await self._mechanical_exit(session, broker, strategy, p, price)
                if result is not None:
                    results.append(result)
            except Exception:  # noqa: BLE001 - one position must not stop the others
                session.rollback()
                log.exception("exit check failed for position %s (%s)", pid, market)
            finally:
                lock.release()
        if unprotected:
            log.error(
                "%d open position(s) are NOT protected by stop-loss/take-profit, their mode "
                "cannot trade now: %s",
                len(unprotected),
                ", ".join(f"{u['mode']} {u['market']} #{u['id']}" for u in unprotected),
            )
        self.unprotected = unprotected
        return results

    # -- reconciliation --

    async def reconcile(self, session: Session) -> int:
        """Settle orders whose outcome is unknown and release stale market claims.

        Runs at startup and before every exit check. Returns the number of orders settled.
        """
        settled = 0
        orders = list(
            session.scalars(
                select(Order)
                .where(Order.status.in_(risk.UNSETTLED_ORDER_STATUSES))
                .order_by(Order.id)
            )
        )
        strategy = load_strategy(session) if orders else None
        for order in orders:
            broker = self.broker_for(order.mode)
            if broker is None:
                log.error(
                    "order %s (%s %s) is unsettled but %s cannot trade now",
                    order.id,
                    order.mode,
                    order.market,
                    order.mode,
                )
                continue
            async with self.market_lock(order.market):
                try:
                    if not await broker.settle(session, order):
                        continue
                    self._book_settled(session, order, strategy)
                    session.commit()
                    settled += 1
                    log.info("order %s settled: %s", order.id, order.status)
                except BitvavoError as exc:
                    session.rollback()
                    log.warning("order %s could not be settled yet: %s", order.id, exc)
                except Exception:  # noqa: BLE001
                    session.rollback()
                    log.exception("settling order %s failed", order.id)
        self._release_stale_claims(session)
        return settled

    def _book_settled(self, session: Session, order: Order, strategy: Strategy | None) -> None:
        position = session.get(Position, order.position_id) if order.position_id else None
        strategy = strategy or load_strategy(session)
        if order.side == "buy":
            if position is not None and position.status == "opening":
                self._book_buy(session, position, order, strategy, order.price or 0.0)
            elif (order.amount or 0) > 0:
                if open_position(session, order.mode, order.market) is not None:
                    log.error(
                        "order %s filled %.8f %s but a position is already open; not tracked",
                        order.id,
                        order.amount,
                        order.market,
                    )
                    return
                claim = Position(
                    mode=order.mode,
                    market=order.market,
                    status="opening",
                    amount=0.0,
                    entry_price=order.price or 0.0,
                    entry_quote=0.0,
                )
                session.add(claim)
                session.flush()
                self._book_buy(session, claim, order, strategy, order.price or 0.0)
                order.position_id = claim.id
        elif position is not None and position.status == "closing":
            self._book_sell(
                session,
                position,
                order,
                order.price or position.entry_price,
                (order.reason or "reconcile")[:32],
            )

    def _release_stale_claims(self, session: Session) -> None:
        cutoff = datetime.now(UTC) - CLAIM_STALE
        busy = set(
            session.scalars(
                select(Order.position_id).where(
                    Order.status.in_(risk.UNSETTLED_ORDER_STATUSES), Order.position_id.is_not(None)
                )
            )
        )
        rows = session.scalars(
            select(Position).where(Position.status.in_(("opening", "closing")))
        ).all()
        changed = False
        for p in rows:
            if p.id in busy:
                continue
            since = p.opened_at if p.status == "opening" else (p.closed_at or p.opened_at)
            if since is not None and since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            if since is not None and since > cutoff:
                continue
            if p.status == "opening":
                log.warning("releasing stale buy claim %s in %s", p.id, p.market)
                session.delete(p)
            else:
                log.warning("releasing stale sell claim %s in %s; open again", p.id, p.market)
                p.status, p.closed_at = "open", None
            changed = True
        if changed:
            session.commit()

    # -- manual orders --

    async def manual_order(
        self,
        session: Session,
        market: str,
        side: str,
        amount_quote: Any,
        reason: str | None = None,
    ) -> Order:
        """A manual order from the dashboard or the API, under the same rules as the bot.

        A buy opens a position (protected by the strategy's stops) and is refused while one is
        open in that market, while the kill switch is on, or when a hard limit says no. A sell
        reduces or closes the open position of that market. Raises ManualOrderError.
        """
        market = str(market or "").strip().upper()
        if market not in self.markets:
            raise ManualOrderError(f"{market or 'markt'} staat niet in MARKETS")
        if side not in ("buy", "sell"):
            raise ManualOrderError("kies kopen (buy) of verkopen (sell)")
        try:
            amount = float(amount_quote)
        except (TypeError, ValueError) as exc:
            raise ManualOrderError("het bedrag is geen geldig getal") from exc
        if not math.isfinite(amount):
            raise ManualOrderError("het bedrag is geen geldig getal")
        if amount < EXCHANGE_MIN_ORDER_EUR:
            raise ManualOrderError(f"minimaal € {EXCHANGE_MIN_ORDER_EUR:.2f} per order")
        if amount > self.limits.max_trade_eur:
            raise ManualOrderError(
                f"maximaal € {self.limits.max_trade_eur:.2f} per order (maximum per order)"
            )
        label = f"manual: {reason[:200]}" if reason else "manual"
        async with self.market_lock(market):
            broker = self.broker
            mode = broker.mode
            if risk.has_unsettled_order(session, mode, market):
                raise ManualOrderError(
                    f"er staat nog een order in {market} waarvan de uitkomst niet bekend is"
                )
            position = open_position(session, mode, market)
            if side == "buy":
                if risk.is_paused(session):
                    raise ManualOrderError("handelen is gepauzeerd (kill switch); kopen kan niet")
                if position is not None:
                    raise ManualOrderError(
                        f"er staat al een open positie in {market}; verkoop die eerst"
                    )
                prices = await self.client.ticker_prices(sorted({*self.markets, market}))
                price = prices.get(market) or await self.client.ticker_price(market)
                total, exposure = await self.portfolio(session, prices, broker)
                allowed, blocked = risk.check_buy(
                    session,
                    self.limits,
                    mode,
                    market,
                    amount,
                    total,
                    exposure,
                    unrealized=risk.unrealized_pnl(session, mode, prices),
                )
                if blocked:
                    raise ManualOrderError(blocked)
                if allowed + 0.005 < amount:
                    raise ManualOrderError(
                        f"de limieten staan nu maximaal € {allowed:.2f} toe in {market}"
                    )
                strategy = load_strategy(session)
                order, _, blocked = await self._buy(
                    session, broker, strategy, market, amount, price, label
                )
                session.commit()
                if blocked:
                    raise ManualOrderError(blocked)
                return order
            if position is None:
                raise ManualOrderError(f"er is geen open positie in {market} om te verkopen")
            price = await self.client.ticker_price(market)
            coins = min(amount / price, position.amount)
            if (position.amount - coins) * price < EXCHANGE_MIN_ORDER_EUR:
                coins = position.amount  # the rest could never be sold on its own
            order, blocked, _ = await self._sell(
                session, broker, position, price, "manual", amount=coins
            )
            session.commit()
            if blocked:
                raise ManualOrderError(blocked)
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
