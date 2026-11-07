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
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.decision_maker import DecisionMaker, TradeDecision
from app.ai.evaluator import Evaluator
from app.bitvavo.broker import Broker, OrderUncertain
from app.bitvavo.client import BitvavoClient, BitvavoError
from app.db import Decision, Evaluation, NewsItem, Order, Position, Setting
from app.news.service import load_etf_flows, load_market_mood, market_sentiment, outlet_of
from app.news.sources import ETF_ASSETS
from app.strategy.describe import ConditionCheck, explain_signal, levels_summary, signal_reason_nl
from app.strategy.engine import SOFT_FAIL_SIZE_FACTOR, backtest, compute_series, signal_at
from app.strategy.model import DEFAULT_STRATEGY, LOOKBACK_OPS, Strategy
from app.strategy.store import StrategySlot, assign_markets, exit_slot_for, load_slots
from app.trading import risk
from app.trading.events import MarketEvent, active_event, upcoming_events
from app.trading.health import describe_problem
from app.trading.journal import JournalFilter, journal_entries, notes_for_claude
from app.trading.plan import load_plan
from app.trading.risk import RiskLimits
from app.trading.shock import ShockGuard
from app.trading.trend import load_trend

log = logging.getLogger(__name__)


def plain_number(value: float) -> str:
    """A float in full, as plain digits without an exponent: 66091.5 -> '66091.5',
    4.056e-06 -> '0.000004056'."""
    return format(Decimal(repr(float(value))), "f")


# News alone may trigger a Claude consult: a strongly positive, high-impact story can open a
# (smaller) position, a strongly negative one can close it. Scores run from -1 to 1.
NEWS_ENTRY_MIN_SCORE = 0.4
NEWS_EXIT_MAX_SCORE = -0.3
# Also used when a soft strategy filter does not hold; the backtest applies the same factor.
NEWS_ENTRY_SIZE_FACTOR = SOFT_FAIL_SIZE_FACTOR
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
SHOCK_SCAN_TIMEOUT_S = 150.0  # the news-shock check before a cycle (at most one Claude call)
DECISION_TIMEOUT_S = 150.0
MARKET_RUN_TIMEOUT_S = 300.0
EXIT_LOCK_WAIT_S = 30.0
# After an order is refused, no new Claude consult in that market for this long.
ORDER_BACKOFF_MIN = 60
# Follow-up after Claude held a buy signal to wait for the turn: for how long the bot offers
# the market again once the turn condition holds, and how far the price may have risen since.
REVISIT_MAX_MIN = 180
REVISIT_MAX_RISE_PCT = 2.0
# An "opening"/"closing" claim without a live order behind it is released after this long.
CLAIM_STALE = timedelta(minutes=10)
ENGINE_STATE_KEY = "engine_state"
# A mechanical exit that keeps failing (no coins on the account, a refused sell) is journaled
# at most once per position per this period; the retries in between are only logged.
EXIT_FAILURE_JOURNAL = timedelta(minutes=60)
STRATEGY_INVALID_BLOCK = "opgeslagen strategie ongeldig"
NO_STRATEGY_REASON = "geen strategie voor deze markt"
STRATEGY_BUDGET_BLOCK = "budget van de strategie is op"
_EXIT_NL = {
    "stop_loss": "stop-loss",
    "trailing_stop": "trailing stop",
    "take_profit": "take-profit",
}
# The exit check's last success is written to the database at most this often (it runs every
# few minutes); a change between failing and working is written straight away.
EXIT_OK_SAVE_EVERY = timedelta(minutes=10)
# An order whose outcome is still unknown after this long is reported to the owner.
ORDER_UNKNOWN_NOTIFY_AFTER = timedelta(minutes=5)
# Notifications about the same thing at most this often (see app/notify.py).
NOTIFY_REPEAT = timedelta(minutes=60)


class ManualOrderError(ValueError):
    """A manual order that the rules do not allow; the message is Dutch, for the owner."""


class EvaluationBusy(RuntimeError):
    """An evaluation is already running; a second one would only spend the budget twice."""


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
    session: Session,
    mode: str,
    days: int = 30,
    limit: int = 200,
    before: tuple[datetime, int] | None = None,
) -> list[Position]:
    """Closed positions of the last ``days``, newest first; ``before`` = (closed_at, id) of the
    last one of the previous page."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = select(Position).where(
        Position.mode == mode, Position.status == "closed", Position.closed_at >= cutoff
    )
    if before is not None:
        at, pid = before
        stmt = stmt.where(
            or_(Position.closed_at < at, and_(Position.closed_at == at, Position.id < pid))
        )
    return list(
        session.scalars(stmt.order_by(Position.closed_at.desc(), Position.id.desc()).limit(limit))
    )


def recent_decisions(
    session: Session,
    mode: str,
    limit: int = 50,
    days: int | None = None,
    before_id: int | None = None,
) -> list[Decision]:
    stmt = select(Decision).where(Decision.mode == mode)
    if days is not None:
        stmt = stmt.where(Decision.created_at >= datetime.now(UTC) - timedelta(days=days))
    if before_id is not None:
        stmt = stmt.where(Decision.id < before_id)
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
            NewsItem.published_at <= now,
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
        # The same outlet identity as the sentiment and /news: an RSS feed is one outlet
        # wherever its links point, one publisher through two aggregators is one outlet.
        best = max(best, len({outlet_of(m) for m in members}))
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
    # The strategy's own estimate (warm-up plus history) is a floor as well.
    needed = max(LIVE_CANDLES_MIN, 2 * longest + lookback + 2, strategy.candles_needed())
    return min(LIVE_CANDLES_MAX, needed)


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


# The ETF-flow labels (app.news.sources.etf_flow_label) each RISK_OFF_ETF_OUTFLOW mode blocks on.
ETF_OUTFLOW_LABELS: dict[str, tuple[str, ...]] = {
    "off": (),
    "strong": ("strong_outflow",),
    "any": ("outflow", "strong_outflow"),
}


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
        shock_assessor: Any = None,
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
        self.market_mood_enabled = True  # FEAR_GREED_ENABLED: pass the stored index to Claude
        self.etf_flows_enabled = True  # ETF_FLOWS_ENABLED: pass the stored ETF flows to Claude
        # Live only: EUR the bot manages (0 = the whole account), see portfolio().
        self.live_capital_eur = 0.0
        # Risk-off: no new buys around calendar events and after a news shock.
        self.calendar: list[MarketEvent] = []
        self.risk_off_enabled = True
        # RISK_OFF_ETF_OUTFLOW: no new buys in a coin while its ETF flows show (strong) outflow.
        self.risk_off_etf_outflow = "strong"
        self.risk_off_before_min = 60
        self.risk_off_after_min = 120
        # News shocks (app/trading/shock.py): Claude judges new vs repeat ("claude") or a fixed
        # pause ("fixed", also the fallback without Claude).
        self.shock_mode = "claude"
        self.shock_min_hours = 1.0
        self.shock_max_hours = 48.0
        self.shock_recheck_min = 60
        self.shock_pause_hours = 2
        self.shock_assessor = shock_assessor
        self.shock = ShockGuard(self)
        self.last_run: datetime | None = None
        self.last_results: list[dict[str, Any]] = []
        self.last_error: str | None = None
        # Dip watch: markets where the dip conditions hold but the turn has not come yet.
        self.watch_max_min = watch_max_min
        self.watch: dict[str, dict[str, Any]] = {}
        self.watch_cooldown: dict[str, datetime] = {}
        self.last_watch_run: datetime | None = None
        # Follow-up: markets where Claude held a buy signal to wait for the turn (per market:
        # since, the price and the condition values at that moment). The next cycles offer
        # the market again as soon as the turn condition holds, see _follow_up().
        self.revisit: dict[str, dict[str, Any]] = {}
        # After a refused order: no new Claude consult in that market until this moment.
        self.order_backoff: dict[str, datetime] = {}
        # Exit loop: a broker for positions of the other mode (set by the app), and the open
        # positions it could not protect on its last run.
        self.broker_for_mode: Callable[[str], Broker | None] | None = None
        self.unprotected: list[dict[str, Any]] = []
        self.last_exit_run: datetime | None = None
        # Health of the exit loop: the last run that really checked every open position (or
        # had none to check), and since when / why it fails (None while it works).
        self.last_exit_ok: datetime | None = None
        self.exit_failing_since: datetime | None = None
        self.exit_error: str | None = None
        self._exit_ok_saved: datetime | None = None
        # Health of the trading cycle: why the last one failed as a whole (Bitvavo unreachable,
        # every market failed) and since when; None after a cycle that worked.
        self.cycle_error: str | None = None
        self.cycle_error_at: datetime | None = None
        self.cycle_failing_since: datetime | None = None
        self.started_at = datetime.now(UTC)
        # Last known prices and portfolio figures, for the pages' risk meters (no extra
        # Bitvavo calls on a page view).
        self.last_prices: dict[str, float] = {}
        self.last_prices_at: datetime | None = None
        self.last_portfolio: dict[str, Any] | None = None
        # Optional channel to the owner's phone (app/notify.py); None = no notifications.
        self.notifier: Any = None
        # Failed mechanical exits per position id: when last journaled and why (persisted).
        self.exit_failures: dict[int, dict[str, Any]] = {}
        # True while the stored strategy does not validate: exits use the default strategy,
        # but no new positions are opened with a strategy the owner never chose.
        self.strategy_invalid = False
        self._locks: dict[str, asyncio.Lock] = {}
        # Held from the limit checks of a buy until it is booked, across markets: the global
        # limits (trades per day, exposure, daily loss) are read before either buy commits.
        # Always taken inside a market lock, never the other way round.
        self._buy_lock = asyncio.Lock()
        self._evaluation_lock = asyncio.Lock()
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
        """Persist the dip watch, its cooldown, the order backoff, the failing exits and when
        the last cycle and exit check ran (best-effort)."""
        state = {
            "last_run": _iso(self.last_run) if self.last_run else None,
            "last_exit_ok": _iso(self.last_exit_ok) if self.last_exit_ok else None,
            "watch": {
                m: {
                    **{k: v for k, v in w.items() if k not in ("since", "last_check")},
                    "since": _iso(w["since"]),
                    "last_check": _iso(w.get("last_check") or w["since"]),
                }
                for m, w in self.watch.items()
            },
            "watch_cooldown": {m: _iso(t) for m, t in self.watch_cooldown.items()},
            "revisit": {m: {**r, "since": _iso(r["since"])} for m, r in self.revisit.items()},
            "order_backoff": {m: _iso(t) for m, t in self.order_backoff.items()},
            "exit_failures": {
                str(pid): {"at": _iso(f["at"]), "reason": f.get("reason")}
                for pid, f in self.exit_failures.items()
            },
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
            revisit = {}
            for market, r in (state.get("revisit") or {}).items():
                since = _from_iso(r.get("since")) if isinstance(r, dict) else None
                if since is None or now - since > timedelta(minutes=REVISIT_MAX_MIN):
                    continue
                try:
                    price = float(r.get("price"))
                except (TypeError, ValueError):
                    continue
                values = r.get("values") if isinstance(r.get("values"), dict) else {}
                revisit[market] = {"since": since, "price": price, "values": values}
            backoff = {
                m: t
                for m, v in (state.get("order_backoff") or {}).items()
                if (t := _from_iso(v)) is not None and t > now
            }
            failures = {}
            for pid, f in (state.get("exit_failures") or {}).items():
                at = _from_iso(f.get("at")) if isinstance(f, dict) else None
                if at is not None and str(pid).isdigit():
                    failures[int(pid)] = {"at": at, "reason": str(f.get("reason") or "")[:200]}
            # A moment in the future (a clock that went back) is not believed.
            last_run = _from_iso(state["last_run"]) if state.get("last_run") else None
            last_exit_ok = _from_iso(state["last_exit_ok"]) if state.get("last_exit_ok") else None
            last_run = last_run if last_run is not None and last_run <= now else None
            last_exit_ok = (
                last_exit_ok if last_exit_ok is not None and last_exit_ok <= now else None
            )
        except Exception:  # noqa: BLE001
            log.exception("ignoring unreadable engine state")
            return
        self.watch, self.watch_cooldown, self.order_backoff = watch, cooldown, backoff
        self.revisit = revisit
        self.exit_failures = failures
        self.last_run = self.last_run or last_run
        self.last_exit_ok = self.last_exit_ok or last_exit_ok
        self._exit_ok_saved = self.last_exit_ok

    # -- notifications --

    def notify(
        self,
        event: str,
        text: str,
        *,
        key: str | None = None,
        every: timedelta | None = NOTIFY_REPEAT,
    ) -> None:
        """Tell the owner outside the app (app/notify.py), rate-limited per ``key``. Never
        raises and never waits: a broken channel must not stop trading or the exits."""
        if self.notifier is None:
            return
        try:
            self.notifier.send(event, text, key=key, every=every)
        except Exception:  # noqa: BLE001
            log.exception("notification %s could not be queued", event)

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
            total, exposure = self.live_capital_eur, risk.position_exposure(session, "live", prices)
        else:
            balances = await broker.balances(session)
            total, exposure = self.portfolio_from(session, broker.mode, balances, prices)
        self.last_portfolio = {
            "mode": broker.mode,
            "total": total,
            "exposure": exposure,
            "at": datetime.now(UTC),
        }
        return total, exposure

    def portfolio_from(
        self, session: Session, mode: str, balances: dict[str, float], prices: dict[str, float]
    ) -> tuple[float, float]:
        """(total_value_eur, exposure_eur) from balances already fetched; see portfolio()."""
        if mode == "live" and self.live_capital_eur > 0:
            return self.live_capital_eur, risk.position_exposure(session, "live", prices)
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

    def remember_prices(self, prices: dict[str, float]) -> None:
        if prices:
            self.last_prices = {**self.last_prices, **prices}
            self.last_prices_at = datetime.now(UTC)

    async def refresh_risk(
        self, session: Session, prices: dict[str, float], broker: Broker | None = None
    ) -> None:
        """Update the portfolio figures behind the risk meters and report a daily loss limit
        that is reached (once per day). Best-effort: never fails the caller."""
        broker = broker or self.broker
        try:
            total, exposure = await self.portfolio(session, prices, broker)
            mode = broker.mode
            day = risk.realized_pnl_today(session, mode) + min(
                risk.unrealized_pnl(session, mode, prices), 0.0
            )
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.warning("refreshing the risk figures failed: %s", exc)
            return
        limit = self.limits.daily_loss_limit_pct
        if total > 0 and -day / total * 100 >= limit:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            where = "live" if mode == "live" else "oefenmodus"
            pct = f"{limit:g}".replace(".", ",")
            self.notify(
                "daily_loss_limit",
                f"De dagverlieslimiet van {pct}% is bereikt ({where}). De bot koopt niets "
                "meer tot 00:00 UTC; stop-loss, trailing stop en take-profit blijven werken.",
                key=f"daily_loss_limit:{mode}:{today}",
                every=timedelta(hours=24),
            )

    # -- one cycle --

    async def _guarded_market(
        self,
        session: Session,
        slot: StrategySlot,
        market: str,
        prices: dict[str, float],
        watch: bool = False,
        consult: bool = False,
    ) -> CycleResult:
        try:
            return await asyncio.wait_for(
                self.run_market(session, slot, market, prices, watch=watch, consult=consult),
                self.market_timeout_s,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"{market}: afgebroken, geen resultaat binnen de tijdslimiet"
            ) from exc

    def _trading_plan(self, session: Session) -> tuple[dict[str, StrategySlot], list[StrategySlot]]:
        """(market -> the strategy that trades it, every strategy) for a cycle. Flags a
        switched-on strategy whose stored rules do not validate: the default strategy stands
        in for its exits, and no new positions are opened in its markets."""
        slots = load_slots(session)
        self.strategy_invalid = any(s.enabled and s.warning is not None for s in slots)
        return assign_markets(slots, self.markets), slots

    def strategy_slots(self, session: Session) -> list[dict[str, Any]]:
        """Per strategy: what it trades and how much of its budget is in use (for the pages
        and the API, in the active mode)."""
        slots = load_slots(session)
        plan = assign_markets(slots, self.markets)
        prices = self.last_prices or {}
        out = []
        for slot in slots:
            markets = [m for m, s in plan.items() if s.id == slot.id]
            invested, room = risk.budget_room(session, self.mode, markets, slot.budget_eur, slot.id)
            positions = (
                [p for p in open_positions(session, self.mode) if p.market in markets]
                if markets
                else []
            )
            value = sum(
                p.amount * prices[p.market] if prices.get(p.market) else p.entry_quote
                for p in positions
            )
            out.append(
                {
                    "slot": slot,
                    "active_markets": markets,
                    "invested_eur": round(invested, 2),
                    "room_eur": None if math.isinf(room) else round(room, 2),
                    "value_eur": round(value, 2),
                    "open_positions": len(positions),
                }
            )
        return out

    def _cycle_failed(self, reason: str) -> None:
        now = datetime.now(UTC)
        self.cycle_error = reason[:300]
        self.cycle_error_at = now
        self.cycle_failing_since = self.cycle_failing_since or now

    async def shock_scan(self, session: Session) -> None:
        """Judge news-shock triggers that came in since the last look, so the cycle acts on
        the latest news (rechecks of running pauses are the scheduler's). Never raises."""
        try:
            await asyncio.wait_for(self.shock.tick(session, rechecks=False), SHOCK_SCAN_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a failed check leaves the pauses as they are
            session.rollback()
            log.exception("news-shock check before the cycle failed")

    async def run_cycle(self, session: Session) -> list[CycleResult]:
        plan, _ = self._trading_plan(session)
        try:
            prices = await self.client.ticker_prices(self.markets)
        except Exception as exc:
            # Nothing ran: say so on the pages (the caller logs or shows the error itself).
            self._cycle_failed(describe_problem(exc))
            raise
        self.remember_prices(prices)
        await self.shock_scan(session)
        results: list[CycleResult] = []
        for market in list(self.markets):
            slot = plan.get(market)
            if slot is None:
                # Tracked, but no strategy trades it: nothing to decide (the exit loop still
                # guards an open position in it). Not journaled: it would repeat every cycle.
                self.watch.pop(market, None)
                results.append(
                    CycleResult(
                        market,
                        prices.get(market, 0.0),
                        "hold",
                        "hold",
                        "rules",
                        False,
                        NO_STRATEGY_REASON,
                    )
                )
                continue
            try:
                results.append(await self._guarded_market(session, slot, market, prices))
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
        failed = [r for r in results if r.signal == "error"]
        if results and len(failed) == len(results):
            first = failed[0]
            self._cycle_failed(f"geen enkele markt lukte ({first.market}: {first.reason or '?'})")
        else:
            self.cycle_error = self.cycle_error_at = self.cycle_failing_since = None
        await self.refresh_risk(session, prices)
        self.save_state(session)
        return results

    # -- dip watch --

    async def run_watch(self, session: Session) -> list[CycleResult]:
        """Re-check only the armed markets; called every WATCH_INTERVAL_MIN by the scheduler."""
        if not self.watch:
            return []
        plan, _ = self._trading_plan(session)
        markets = [m for m in self.watch if m in plan]
        for stale in set(self.watch) - set(markets):
            self.watch.pop(stale, None)
        if not markets:
            return []
        prices = await self.client.ticker_prices(markets)
        self.remember_prices(prices)
        results = []
        for market in markets:
            try:
                results.append(
                    await self._guarded_market(session, plan[market], market, prices, watch=True)
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

    def _follow_up(
        self,
        market: str,
        signal: str,
        position: Position | None,
        risk_off: dict[str, Any] | None,
        price: float,
        turn_checks: list[ConditionCheck],
        now: datetime,
    ) -> dict[str, Any] | None:
        """The pending follow-up for this market when it fires now, else None.

        Claude held a buy signal earlier to wait for the turn. Once the turn condition holds
        (the lookback checks, e.g. RSI rising) and the price has not run away since, the market
        goes to Claude again as a buy, even if the dip condition (RSI below 35) no longer
        holds. Cleared by a position, a plain signal, or after REVISIT_MAX_MIN.
        """
        pending = self.revisit.get(market)
        if pending is None:
            return None
        if (
            position is not None
            or signal == "buy"
            or now - pending["since"] > timedelta(minutes=REVISIT_MAX_MIN)
        ):
            self.revisit.pop(market, None)
            return None
        if risk_off is not None or not turn_checks or not all(c.ok for c in turn_checks):
            return None
        if price > pending["price"] * (1 + REVISIT_MAX_RISE_PCT / 100):
            return None
        self.revisit.pop(market, None)
        return {
            **pending,
            "price_now": price,
            "turn": "; ".join(f"{c.text} ✓ ({c.values_nl})" for c in turn_checks),
        }

    def revisit_status(self) -> list[dict[str, Any]]:
        """The pending follow-ups for the pages: market, since, until, the price then."""
        return [
            {
                "market": m,
                "since": r["since"],
                "until": r["since"] + timedelta(minutes=REVISIT_MAX_MIN),
                "price": r["price"],
                "max_price": round(r["price"] * (1 + REVISIT_MAX_RISE_PCT / 100), 8),
                "values": r.get("values") or {},
            }
            for m, r in sorted(self.revisit.items())
        ]

    async def run_consult(self, session: Session, market: str) -> CycleResult:
        """Ask Claude about one market now, whatever the signal says ('Claude laten
        beoordelen'). Same rules and limits as a cycle: without a buy signal he cannot buy."""
        if market not in self.markets:
            raise ValueError(f"{market} staat niet bij de markten van de bot")
        plan, _ = self._trading_plan(session)
        slot = plan.get(market)
        if slot is None:
            raise ValueError(f"geen strategie handelt op {market}; wijs de markt eerst toe")
        prices = await self.client.ticker_prices([market])
        self.remember_prices(prices)
        result = await self._guarded_market(session, slot, market, prices, consult=True)
        # The market's card on Handelen shows this run as its last one.
        self.last_results = [r for r in self.last_results if r.get("market") != market] + [
            result.as_dict()
        ]
        self.save_state(session)
        return result

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
        pause = self.shock.pause_for(session, market.split("-")[0], now)
        if pause is not None:
            return self.shock.risk_off_info(pause)
        return self.etf_risk_off(session, market.split("-")[0], now)

    def etf_risk_off(self, session: Session, coin: str, now: datetime) -> dict[str, Any] | None:
        """Risk-off on ETF outflow: the stored flows of ``coin`` show outflow of the kind
        RISK_OFF_ETF_OUTFLOW asks for (day figures; the block lasts until they turn)."""
        if not self.risk_off_enabled or not self.etf_flows_enabled:
            return None
        wanted = ETF_OUTFLOW_LABELS.get(self.risk_off_etf_outflow) or ()
        if not wanted:
            return None
        flows = load_etf_flows(session, coin, now)
        if flows is None or flows.get("label") not in wanted:
            return None
        sum_5d = float(flows.get("sum_5d_musd") or 0)
        return {
            "kind": "etf",
            "label": f"ETF-uitstroom {coin}",
            "at": flows.get("as_of"),
            "until": None,
            "sum_5d_musd": flows.get("sum_5d_musd"),
            "streak_days": flows.get("streak_days"),
            "flow_label": flows.get("label"),
            "reason_nl": (
                f"ETF-uitstroom {coin}: {sum_5d:+,.0f} mln USD over 5 dagen "
                f"({flows.get('label_nl')}, stand {flows.get('as_of')}); geen nieuwe aankopen "
                "zolang de stroom niet keert"
            ).replace(",", " "),
        }

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
            "shock_mode": self.shock_mode,
        }

    def events_status(self, session: Session) -> dict[str, Any]:
        now = datetime.now(UTC)
        shocks = {}
        etf_outflows = {}
        for market in self.markets:
            ro = self.risk_off(session, market, now)
            if ro and ro["kind"] == "shock":
                shocks[market] = ro
            elif ro and ro["kind"] == "etf":
                etf_outflows[market] = ro
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
            "etf_outflows": etf_outflows,
            "etf_outflow_mode": self.risk_off_etf_outflow if self.etf_flows_enabled else "off",
            "upcoming": [e.as_dict() for e in upcoming_events(self.calendar, now, 4)],
            "before_min": self.risk_off_before_min,
            "after_min": self.risk_off_after_min,
            "shock_pause_hours": self.shock_pause_hours,
            "shock_mode": self.shock_mode,
            "shock_min_hours": self.shock.min_hours,
            "shock_max_hours": self.shock.max_hours,
            "shock_pauses": self.shock.status(session, now),
            "shock_history": self.shock.history(session, now),
        }

    async def run_market(
        self,
        session: Session,
        slot: StrategySlot | Strategy,
        market: str,
        prices: dict[str, float],
        watch: bool = False,
        consult: bool = False,
    ) -> CycleResult:
        """One market with one strategy: a regular cycle, a dip-watch check (``watch``) or a
        consultation the owner asked for (``consult``: Claude is asked whatever the signal
        says). A bare ``Strategy`` counts as a strategy without a budget of its own."""
        if isinstance(slot, Strategy):
            slot = StrategySlot(id=0, strategy=slot)
        async with self.market_lock(market):
            # One snapshot for the whole run: a mode switch halfway can never make this run
            # trade through the other mode's broker.
            broker = self.broker
            return await self._run_market(session, broker, slot, market, prices, watch, consult)

    async def _run_market(
        self,
        session: Session,
        broker: Broker,
        slot: StrategySlot,
        market: str,
        prices: dict[str, float],
        watch: bool,
        consult: bool = False,
    ) -> CycleResult:
        mode = broker.mode
        strategy = slot.strategy
        # A bare strategy (slot 0, from a caller without a plan) follows the engine-wide flag.
        strategy_invalid = slot.warning is not None or (slot.id == 0 and self.strategy_invalid)
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
        follow_up = None
        if not watch:
            follow_up = self._follow_up(market, signal, position, risk_off, price, turn_checks, now)
            if follow_up is not None:
                signal = "buy"
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
            strategy_id=slot.id or None,
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
            or consult
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
        if strategy_invalid and position is None and (needs_claude or signal == "buy"):
            # 2b'. The stored strategy does not validate: the default stands in for exits only.
            # No new position on a strategy the owner never chose; Claude is not asked.
            self.watch.pop(market, None)
            decision.reasoning = (
                "De opgeslagen strategie is ongeldig; de bot opent geen nieuwe posities tot er "
                "een geldige strategie is opgeslagen. Verkopen en stop-loss blijven werken."
            )
            decision.blocked_by = STRATEGY_INVALID_BLOCK
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
                f"{sentiment.items} nieuwsfeit{'' if sentiment.items == 1 else 'en'} uit "
                f"{sentiment.articles} bericht{'' if sentiment.articles == 1 else 'en'}, "
                f"{sentiment.high_impact} met impact middel of groot)"
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
        action, confidence, amount_quote, reasoning, source, model, revisit = await self._decide(
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
            follow_up=follow_up,
            consult=consult,
            slot=slot,
        )
        if follow_up is not None:
            reasoning = (
                f"Vervolg: Claude wachtte om {follow_up['since']:%H:%M} UTC op de draai. "
                + reasoning
            )
        elif consult:
            reasoning = "Op verzoek beoordeeld. " + reasoning
        if (
            revisit
            and source == "claude"
            and action == "hold"
            and signal == "buy"
            and position is None
            and follow_up is None
        ):
            # Claude waits for the turn to confirm: offer the market again next cycle(s).
            self.revisit[market] = {
                "since": now,
                "price": price,
                "values": {c.text: c.left for c in checks["buy"]},
            }
            log.info("revisit %s armed: Claude waits for the turn", market)
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
                    # Only the kind in blocked_by: a shock's reason carries a news summary,
                    # and blocked_by reaches Claude's prompts. The details go in the reasoning.
                    blocked = f"risk-off: {ro['kind']}"
                    decision.reasoning = f"{decision.reasoning} (risk-off: {ro['reason_nl']})"
                elif strategy_invalid:
                    blocked = STRATEGY_INVALID_BLOCK
                else:
                    async with self._buy_lock:
                        total, exposure = await self.portfolio(session, prices, broker)
                        # The strategy's own budget first (its own markets, entry value),
                        # then the general limits.
                        amount_quote, blocked = self._within_budget(
                            session, slot, mode, amount_quote
                        )
                        allowed = 0.0
                        if blocked is None:
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
                                strategy_id=slot.id or None,
                            )
                            session.commit()  # booked before the next buy checks the limits
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
                    # Only a sell on news alone waits for the kill switch (a signal sell does not).
                    blocked = (
                        "de noodstop staat aan: een verkoop alleen op negatief nieuws wacht tot je "
                        "hervat"
                    )
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
        follow_up: dict | None = None,
        consult: bool = False,
        slot: StrategySlot | None = None,
    ) -> tuple[str, float | None, float, str, str, str | None, bool]:
        """(action, confidence, amount, reasoning, source, model, revisit_next_cycle)."""
        mode = mode or self.mode
        context = {
            "market": market,
            "price": price,
            "strategy": strategy.model_dump(),
            "strategy_budget": self._budget_context(session, slot, mode),
            "signal": signal,
            "news_entry_allowed": news_entry,
            "follow_up": None
            if follow_up is None
            else {
                "held_at": follow_up["since"],
                "price_then": follow_up["price"],
                "values_then": follow_up.get("values") or {},
                "turn_now": follow_up.get("turn"),
                "note": "je wachtte toen op de draai; die bevestiging is er nu, het signaal "
                "telt als koopsignaal ook al geldt de dipvoorwaarde niet meer",
            },
            "owner_request": consult,
            "signal_checks": {k: [c.as_dict() for c in v] for k, v in (checks or {}).items()},
            "soft_filters_failed": [
                c.text for c in (checks or {}).get("buy", []) if c.soft and not c.ok
            ],
            "indicators": indicators,
            "support_levels": levels_summary(strategy, indicators),
            "recent_candles": trajectory or {},
            "news": sentiment.as_dict(),
            # Fear & Greed index, market-wide; nothing once the feed is switched off.
            "market_mood": load_market_mood(session) if self.market_mood_enabled else None,
            "long_term_trend": load_trend(session, market) if self.trend_context else None,
            # Net flows of the US spot ETFs of this coin; None for a coin without ETFs.
            "etf_flows": load_etf_flows(session, market.split("-")[0])
            if self.etf_flows_enabled
            else None,
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
            "limits": {
                "max_trade_eur": self.limits.max_trade_eur,
                **self._budget_limits(session, slot, mode),
            },
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
                self.last_error = None  # Claude answers again: an old failure is history
                return (
                    d.action,
                    d.confidence,
                    min(d.amount_quote, self.limits.max_trade_eur),
                    d.reasoning_nl,
                    "claude",
                    self.decision_maker.model,
                    bool(getattr(d, "revisit_next_cycle", False)),
                )
            except BudgetExceeded as exc:
                self.last_error = str(exc)
                month = datetime.now(UTC).strftime("%Y-%m")
                self.notify(
                    "claude_budget",
                    "Het Claude-budget is op, dus de bot kan Claude niet meer vragen. Bij een "
                    "signaal "
                    + (
                        "volgt hij nu alleen de regels."
                        if self.claude_unavailable_mode == "indicators"
                        else "wacht hij nu af en koopt hij niets."
                    )
                    + " Verhoog het budget of wacht tot volgende maand.",
                    key=f"claude_budget:{month}",
                    every=timedelta(hours=24),
                )
            except TimeoutError:
                self.last_error = (
                    f"Claude-beslissing duurde langer dan {self.decision_timeout_s:.0f} s"
                )
                log.warning("decision for %s timed out", market)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"Claude-beslissing mislukt: {exc}"
                log.exception("decision failed")
        else:
            self.last_error = "er is geen Claude-sleutel"
        return (*self._fallback(signal, position, sentiment), False)

    def _slot_markets(self, slot: StrategySlot) -> list[str]:
        """The tracked markets this strategy trades (where its budget is spent)."""
        if slot.id == 0:
            return list(self.markets)
        if slot.active_markets is not None:
            return [m for m in slot.active_markets if m in self.markets]
        return [m for m in slot.markets if m in self.markets]

    def _budget_state(
        self, session: Session, slot: StrategySlot | None, mode: str
    ) -> tuple[list[str], float, float] | None:
        """(markets, invested, room) of a strategy with a budget of its own, else None."""
        if slot is None or not slot.budget_eur or slot.budget_eur <= 0:
            return None
        markets = self._slot_markets(slot)
        invested, room = risk.budget_room(session, mode, markets, slot.budget_eur, slot.id or None)
        return markets, invested, room

    def _budget_context(
        self, session: Session, slot: StrategySlot | None, mode: str
    ) -> dict[str, Any] | None:
        """What Claude sees of the strategy's own money: its markets, budget and what is
        left; None for a strategy without a budget of its own."""
        if slot is None or slot.id == 0:
            return None
        state = self._budget_state(session, slot, mode)
        if state is None:
            return {
                "strategy_id": slot.id,
                "markets": self._slot_markets(slot),
                "budget_eur": None,
                "note": "deze strategie heeft geen eigen budget; alleen de algemene limieten",
            }
        markets, invested, room = state
        return {
            "strategy_id": slot.id,
            "markets": markets,
            "budget_eur": slot.budget_eur,
            "invested_eur": round(invested, 2),
            "available_eur": round(room, 2),
            "note": "eigen budget van deze strategie: wat in open posities van haar markten "
            "zit telt mee; een aankoop kan nooit groter zijn dan available_eur",
        }

    def _budget_limits(
        self, session: Session, slot: StrategySlot | None, mode: str
    ) -> dict[str, Any]:
        state = self._budget_state(session, slot, mode)
        if state is None:
            return {}
        _, _, room = state
        return {"strategy_available_eur": round(room, 2)}

    def _within_budget(
        self, session: Session, slot: StrategySlot, mode: str, amount_quote: float
    ) -> tuple[float, str | None]:
        """Cap a buy on what the strategy's own budget leaves; (amount, block reason)."""
        state = self._budget_state(session, slot, mode)
        if state is None:
            return amount_quote, None
        _, invested, room = state
        if room < self.limits.min_trade_eur or room < EXCHANGE_MIN_ORDER_EUR:
            return 0.0, (
                f"{STRATEGY_BUDGET_BLOCK} ('{slot.name}': € {invested:.2f} van € "
                f"{slot.budget_eur:.2f} belegd)"
            )
        return min(amount_quote, room), None

    def _fallback(
        self, signal: str, position, sentiment
    ) -> tuple[str, float | None, float, str, str, str | None]:
        """What to do without Claude. A strategy sell signal on an open position is always
        followed (in both modes); ``indicators`` also follows a buy signal, but with half the
        budget and not against strongly negative news. Risk-off is enforced afterwards."""
        why = f"Claude niet beschikbaar: {self.last_error}"
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
        # The price in full (plain digits, no exponent): '0.000004056' for SHIB, so the page
        # can show the stop it hit with significant digits (display._MECHANICAL, UX-29).
        decision.reasoning = f"{reason} geraakt op {plain_number(price)}"
        market, signal, pid = position.market, decision.signal, position.id
        uncertain = False
        try:
            order, blocked, note = await self._sell(session, broker, position, price, reason)
        except OrderUncertain as exc:
            order, blocked, note, uncertain = exc.order, str(exc), None, True
        except (ValueError, BitvavoError) as exc:
            session.rollback()
            order, blocked, note = None, f"verkoop mislukt: {exc}", None
            log.error("%s for %s %s failed: %s", reason, position.mode, market, exc)
        if blocked is not None and not uncertain:
            # The sell did not happen (no coins on the account, a refusal) and the next run
            # tries again: journal that once per period, not on every exit-loop run.
            now = datetime.now(UTC)
            last = self.exit_failures.get(pid)
            where = "live" if position.mode == "live" else "oefenmodus"
            self.notify(
                "exit_sell_failed",
                f"De verkoop bij de {_EXIT_NL.get(reason, reason)} van {market} ({where}, "
                f"positie #{pid}) lukt niet: {blocked[:160]}. De bot probeert het elke "
                "controle opnieuw; verkoop zo nodig zelf op Bitvavo.",
                key=f"exit_sell_failed:{pid}",
            )
            self.exit_failures[pid] = {
                "at": last["at"] if last and now - last["at"] < EXIT_FAILURE_JOURNAL else now,
                "reason": blocked[:200],
            }
            if last is not None and now - last["at"] < EXIT_FAILURE_JOURNAL:
                log.warning("%s for position %s still fails: %s", reason, pid, blocked)
                # Commit, not roll back: an unfilled sell already settled its order and reopened
                # the position in this session. The decision itself is not journaled again.
                if decision in session:
                    session.expunge(decision)
                session.commit()
                return CycleResult(market, price, signal, "sell", "mechanical", False, blocked)
        else:
            self.exit_failures.pop(pid, None)
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
        strategy_id: int | None = None,
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
            strategy_id=strategy_id,
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
            if self._order_in_flight(session, claim.id):
                # The order was written (and maybe sent) before the error: keep the claim,
                # reconcile() settles the order and books or releases it.
                log.error("buy in %s failed after the order was sent; left to reconcile", market)
                raise
            session.delete(claim)
            session.commit()
            raise
        if not self._book_buy(session, claim, order, strategy, price):
            return order, None, f"kooporder niet gevuld (status {order.status})"
        self._notify_live_order(order, reason)
        return order, claim, None

    def _notify_live_order(self, order: Order, reason: str | None) -> None:
        """A live order that (partly) filled: one notification per order, without amounts."""
        if order.mode != "live" or not (order.amount or 0) > 0:
            return
        side = "Gekocht" if order.side == "buy" else "Verkocht"
        why = str(reason or "").split(":", 1)[0].strip()
        why_nl = {
            "claude": "beslissing van Claude",
            "rules": "regels",
            "manual": "handmatige order",
            "signal": "verkoopsignaal",
            "news_exit": "zwaar negatief nieuws",
            "reconcile": "na controle bij Bitvavo",
            **_EXIT_NL,
        }.get(why)
        self.notify(
            "live_order",
            f"{side}: {order.market} (live, echt geld)"
            + (f", reden: {why_nl}" if why_nl else "")
            + f". Order #{order.id}; de bedragen staan in Crypie.",
            key=f"live_order:{order.id}",
            every=None,
        )

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
            if self._order_in_flight(session, position.id):
                # Sent but not settled: reopening would leave a sale reconcile() never books.
                log.error(
                    "sell of position %s failed after the order was sent; left to reconcile",
                    position.id,
                )
                raise
            position.status, position.closed_at = "open", None
            session.commit()
            raise
        sold, closed = self._book_sell(session, position, order, price, reason, qty, full_exit)
        if sold <= 0:
            return order, f"verkooporder niet gevuld (status {order.status})", None
        self._notify_live_order(order, reason)
        if not closed:
            note = (
                f"gedeeltelijk gevuld: {sold:.8f} verkocht, {position.amount:.8f} blijft open "
                "met dezelfde stop"
            )
            return order, None, note
        return order, None, None

    @staticmethod
    def _order_in_flight(session: Session, position_id: int | None) -> bool:
        """True when an order for this position is pending or unknown (sent, outcome open)."""
        if position_id is None:
            return False
        return (
            session.scalar(
                select(Order.id)
                .where(
                    Order.position_id == position_id,
                    Order.status.in_(risk.UNSETTLED_ORDER_STATUSES),
                )
                .limit(1)
            )
            is not None
        )

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

    @staticmethod
    def _exit_strategy(
        position: Position, plan: dict[str, StrategySlot], slots: list[StrategySlot]
    ) -> Strategy:
        """Whose stops guard this position: the strategy that trades its market now, else
        the one that opened it, else the first strategy (see store.exit_slot_for)."""
        slot = exit_slot_for(position.market, position.strategy_id, plan, slots)
        return slot.strategy if slot is not None else DEFAULT_STRATEGY

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
        stuck = self._stuck_claims(session)
        open_ids = {p.id for p in positions}
        failures_before = {pid: f["at"] for pid, f in self.exit_failures.items()}
        for pid in set(self.exit_failures) - open_ids:
            self.exit_failures.pop(pid, None)
        if not positions:
            self.unprotected = stuck
            self._exits_ok(session, force_save=bool(failures_before))
            self._notify_unprotected(stuck)
            return []
        slots = load_slots(session)
        plan = assign_markets(slots, self.markets)
        try:
            prices = await self.client.ticker_prices(sorted({p.market for p in positions}))
        except Exception as exc:  # noqa: BLE001 - reported below, retried next run
            self._exits_failed(session, positions, stuck, exc)
            return []
        self.remember_prices(prices)
        results: list[CycleResult] = []
        unprotected: list[dict[str, Any]] = []

        def unprotect(p: Position, reason: str) -> None:
            unprotected.append(
                {
                    "id": p.id,
                    "mode": p.mode,
                    "market": p.market,
                    "amount": p.amount,
                    "stop_price": p.stop_price,
                    "price": prices.get(p.market),
                    "reason": reason,
                }
            )

        for p in positions:
            pid, mode, market = p.id, p.mode, p.market
            if self.broker_for(mode) is None:
                unprotect(
                    p,
                    "de modus kan nu niet handelen (live zonder Bitvavo-sleutel of "
                    "LIVE_TRADING_ACK)",
                )
                continue
            price = prices.get(market)
            if not price:
                log.warning("exit check: no price for %s, position %s not checked", market, pid)
                unprotect(p, "geen koers voor deze markt (van de beurs gehaald of hernoemd?)")
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
                strategy = self._exit_strategy(p, plan, slots)
                result = await self._mechanical_exit(session, broker, strategy, p, price)
                if result is not None:
                    results.append(result)
                    failure = self.exit_failures.get(pid)
                    if not result.executed and failure is not None:
                        unprotect(p, f"verkoop bij de stop lukt niet: {failure['reason']}")
            except Exception:  # noqa: BLE001 - one position must not stop the others
                session.rollback()
                log.exception("exit check failed for position %s (%s)", pid, market)
            finally:
                lock.release()
        unprotected += stuck
        if unprotected:
            log.error(
                "%d position(s) are NOT protected by stop-loss/take-profit: %s",
                len(unprotected),
                ", ".join(
                    f"{u['mode']} {u['market']} #{u['id']} ({u['reason']})" for u in unprotected
                ),
            )
        self.unprotected = unprotected
        # Saved when a failure changed, so a restart does not journal the same failure again.
        changed = {pid: f["at"] for pid, f in self.exit_failures.items()} != failures_before
        self._exits_ok(session, force_save=changed)
        self._notify_unprotected(unprotected)
        if any(r.executed for r in results):
            await self.refresh_risk(session, {**self.last_prices, **prices})
        return results

    def _exits_ok(self, session: Session, force_save: bool = False) -> None:
        """The exit check ran through: remember when (persisted at most every few minutes)."""
        now = datetime.now(UTC)
        recovered = self.exit_failing_since is not None
        if recovered:
            log.info("exit check works again (failing since %s)", self.exit_failing_since)
        self.last_exit_ok = now
        self.exit_failing_since = self.exit_error = None
        stale = self._exit_ok_saved is None or now - self._exit_ok_saved >= EXIT_OK_SAVE_EVERY
        if force_save or recovered or stale:
            self.save_state(session)
            self._exit_ok_saved = now

    def _exits_failed(
        self,
        session: Session,
        positions: list[Position],
        stuck: list[dict[str, Any]],
        exc: Exception,
    ) -> None:
        """The exit check could not check the stops (no prices from Bitvavo): every open
        position counts as unprotected until it works again, with the reason and since when."""
        now = datetime.now(UTC)
        first = self.exit_failing_since is None
        self.exit_failing_since = self.exit_failing_since or now
        self.exit_error = describe_problem(exc)
        since = self.exit_failing_since
        why = (
            f"de koers is niet op te halen sinds {since:%H:%M} UTC; stop-loss, trailing stop en "
            "take-profit worden niet gecontroleerd"
        )
        self.unprotected = [
            {
                "id": p.id,
                "mode": p.mode,
                "market": p.market,
                "amount": p.amount,
                "stop_price": p.stop_price,
                "price": self.last_prices.get(p.market),
                "reason": why,
                "since": since,
            }
            for p in positions
        ] + stuck
        log.error(
            "exit check failed, %d open position(s) NOT protected since %s: %s",
            len(positions),
            f"{since:%H:%M} UTC",
            exc,
        )
        if first:
            self.save_state(session)  # the last success stays what it was
        minutes = getattr(self.notifier, "exit_fail_min", 10)
        if now - since >= timedelta(minutes=minutes):
            n = len(positions)
            self.notify(
                "exit_check_failing",
                f"De bewaking van stop-loss en take-profit lukt al sinds {since:%H:%M} UTC niet: "
                f"de koersen zijn niet op te halen bij Bitvavo. {n} open "
                + ("positie wordt" if n == 1 else "posities worden")
                + " nu niet bewaakt. Controleer Bitvavo en verkoop zo nodig zelf.",
                key="exit_check_failing",
            )

    def _notify_unprotected(self, unprotected: list[dict[str, Any]]) -> None:
        if not unprotected:
            return
        ids = ",".join(str(u["id"]) for u in sorted(unprotected, key=lambda u: u["id"]))
        n = len(unprotected)
        lines = "; ".join(
            f"{u['market']} ({'live' if u['mode'] == 'live' else 'oefenmodus'}, "
            f"#{u['id']}): {u['reason']}"
            for u in unprotected[:5]
        )
        self.notify(
            "unprotected",
            f"{n} " + ("positie wordt" if n == 1 else "posities worden") + " NIET bewaakt door "
            f"stop-loss of take-profit. {lines}. Los het op of verkoop zelf op Bitvavo.",
            key=f"unprotected:{ids}",
        )

    async def exit_check(self, session: Session) -> list[CycleResult]:
        """One run of the exit loop: settle unknown orders, then check every open position.

        A failing settlement does not skip the stop checks; it is logged and reported.
        """
        try:
            await self.reconcile(session)
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("order reconciliation failed; checking the stops anyway")
        return await self.run_exits(session)

    def _stuck_claims(self, session: Session) -> list[dict[str, Any]]:
        """Positions stuck in "opening"/"closing" past CLAIM_STALE, because an order behind
        them stays unsettled: no stop check runs on them and their market is blocked."""
        cutoff = datetime.now(UTC) - CLAIM_STALE
        out = []
        rows = session.scalars(
            select(Position).where(Position.status.in_(("opening", "closing")))
        ).all()
        for p in rows:
            since = p.opened_at if p.status == "opening" else (p.closed_at or p.opened_at)
            if since is not None and since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            if since is None or since > cutoff:
                continue
            out.append(
                {
                    "id": p.id,
                    "mode": p.mode,
                    "market": p.market,
                    "amount": p.amount,
                    "stop_price": p.stop_price,
                    "price": None,
                    "reason": (
                        ("aankoop" if p.status == "opening" else "verkoop")
                        + " met onbekende uitkomst sinds "
                        + f"{since:%Y-%m-%d %H:%M} UTC; controleer de order op Bitvavo"
                    ),
                }
            )
        return out

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
        slots = load_slots(session) if orders else []
        plan = assign_markets(slots, self.markets) if orders else {}
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
                    self._book_settled(session, order, plan, slots)
                    session.commit()
                    settled += 1
                    log.info("order %s settled: %s", order.id, order.status)
                except BitvavoError as exc:
                    session.rollback()
                    log.warning("order %s could not be settled yet: %s", order.id, exc)
                except Exception:  # noqa: BLE001
                    session.rollback()
                    log.exception("settling order %s failed", order.id)
        self._notify_unsettled(session)
        self._release_stale_claims(session)
        return settled

    def _notify_unsettled(self, session: Session) -> None:
        """Orders that stay without a known outcome for a while: tell the owner once each."""
        if self.notifier is None or not getattr(self.notifier, "enabled", True):
            return
        cutoff = datetime.now(UTC) - ORDER_UNKNOWN_NOTIFY_AFTER
        rows = session.scalars(
            select(Order).where(Order.status.in_(risk.UNSETTLED_ORDER_STATUSES)).order_by(Order.id)
        ).all()
        for order in rows:
            created = order.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created is None or created > cutoff:
                continue
            side = "kooporder" if order.side == "buy" else "verkooporder"
            where = "live" if order.mode == "live" else "oefenmodus"
            self.notify(
                "order_unknown",
                f"Van een {side} in {order.market} ({where}, order #{order.id}, geplaatst om "
                f"{created:%H:%M} UTC) is de uitkomst nog niet bekend. De bot koopt in die markt "
                "niets tot dat duidelijk is. Controleer de order op Bitvavo.",
                key=f"order_unknown:{order.id}",
                every=timedelta(hours=24),
            )

    def _book_settled(
        self,
        session: Session,
        order: Order,
        plan: dict[str, StrategySlot] | None = None,
        slots: list[StrategySlot] | None = None,
    ) -> None:
        position = session.get(Position, order.position_id) if order.position_id else None
        if slots is None:
            slots = load_slots(session)
            plan = assign_markets(slots, self.markets)
        owner = exit_slot_for(
            order.market, position.strategy_id if position else None, plan or {}, slots
        )
        strategy = owner.strategy if owner is not None else DEFAULT_STRATEGY
        self._notify_live_order(order, order.reason or "reconcile")  # a late (partial) fill
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
                    strategy_id=owner.id if owner is not None else None,
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
        # The per-order maximum limits new exposure; a sell only reduces it and is bounded by
        # the position, so one sell can always close a position in full.
        if side == "buy" and amount > self.limits.max_trade_eur:
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
                self.remember_prices(prices)
                price = prices.get(market) or await self.client.ticker_price(market)
                # The strategy that trades this market sets the stops and lends its budget;
                # a market without one gets the first strategy's stops and no budget.
                plan, slots = self._trading_plan(session)
                slot = plan.get(market)
                strategy = slot.strategy if slot is not None else slots[0].strategy
                async with self._buy_lock:  # inside the market lock, like _run_market
                    total, exposure = await self.portfolio(session, prices, broker)
                    if slot is not None:
                        capped, blocked = self._within_budget(session, slot, mode, amount)
                        if blocked:
                            raise ManualOrderError(blocked)
                        if capped + 0.005 < amount:
                            raise ManualOrderError(
                                f"het budget van strategie '{slot.name}' laat nu hoogstens "
                                f"€ {capped:.2f} toe in {market}"
                            )
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
                    order, _, blocked = await self._buy(
                        session,
                        broker,
                        strategy,
                        market,
                        amount,
                        price,
                        label,
                        strategy_id=slot.id if slot is not None else None,
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

    @property
    def evaluation_busy(self) -> bool:
        return self._evaluation_lock.locked()

    def _etf_flows_for_evaluation(self, session: Session, days: int) -> dict[str, Any]:
        """Per coin with ETFs: the stored summary, its daily series cut to the period."""
        since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        out: dict[str, Any] = {}
        for market in self.markets:
            coin = market.split("-")[0]
            if coin in out:
                continue
            flows = load_etf_flows(session, coin)
            if flows is None:
                if coin in ETF_ASSETS:
                    out[coin] = None
                continue
            daily = [d for d in flows.get("daily") or [] if str(d.get("date")) >= since]
            out[coin] = {
                k: flows.get(k)
                for k in (
                    "as_of",
                    "last_day_musd",
                    "sum_5d_musd",
                    "sum_30d_musd",
                    "typical_day_musd",
                    "streak_days",
                    "label",
                    "label_nl",
                    "source",
                )
            }
            out[coin]["daily_in_period"] = daily
            out[coin]["note"] = (
                "dagelijkse nettostroom van de Amerikaanse spot-ETF's in miljoenen USD "
                "(positief = geld erin); context, geen indicator van de strategie"
            )
        return out

    async def run_evaluation(self, session: Session, days: int = 7) -> Evaluation:
        """One evaluation at a time (button, API and scheduler); a second request while one
        runs raises EvaluationBusy instead of paying Claude twice."""
        if self._evaluation_lock.locked():
            raise EvaluationBusy("Er loopt al een evaluatie; probeer het straks opnieuw.")
        async with self._evaluation_lock:
            return await self._run_evaluation(session, days)

    async def _run_evaluation(self, session: Session, days: int) -> Evaluation:
        slots = load_slots(session)
        plan = assign_markets(slots, self.markets)
        strategy = slots[0].strategy
        closed = closed_positions(session, self.mode, days)
        decisions = [d for d in recent_decisions(session, self.mode, 200) if d.source != "rules"]
        context = {
            "period_days": days,
            "mode": self.mode,
            "strategy": strategy.model_dump(),
            # Every strategy with its id, so a proposal can say which one it changes.
            "strategies": [
                {
                    "strategy_id": s.id,
                    "enabled": s.enabled,
                    "markets": [m for m, owner in plan.items() if owner.id == s.id],
                    "budget_eur": s.budget_eur or None,
                    "strategy": s.strategy.model_dump(),
                }
                for s in slots
            ],
            "performance": performance_summary(session, self.mode, days),
            "closed_positions": [
                {
                    "market": p.market,
                    "strategy_id": p.strategy_id,
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
                    "strategy_id": d.strategy_id,
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
            # Net flows of the US spot ETFs per coin over the period (day figures, millions
            # of USD), so the evaluation can relate decisions to institutional buying/selling.
            "etf_flows": self._etf_flows_for_evaluation(session, days)
            if self.etf_flows_enabled
            else None,
        }
        result = await self.evaluator.evaluate(session, context)
        proposal_json = None
        backtest_json = None
        target_id = None
        if result.proposed_strategy is not None:
            proposal_json = result.proposed_strategy.model_dump_json()
            # The strategy the proposal changes: the one Claude names, else the first.
            wanted = getattr(result, "proposed_for_strategy_id", None)
            target = next((s for s in slots if s.id == wanted), None) or slots[0]
            target_id = target.id
            current = target.strategy
            try:
                candles = await self.client.candles(
                    result.proposed_strategy.market, result.proposed_strategy.interval, 500
                )
                backtest_json = json.dumps(
                    {
                        "proposed": backtest(
                            result.proposed_strategy, candles, 1000, self.backtest_fee_pct
                        ).summary(),
                        "current": backtest(current, candles, 1000, self.backtest_fee_pct).summary()
                        if current.market == result.proposed_strategy.market
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
            strategy_id=target_id,
        )
        session.add(ev)
        session.commit()
        session.refresh(ev)
        return ev
