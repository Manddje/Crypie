"""News shocks: when the bot stops buying after heavy news, and when it starts again.

A **trigger** is an article of the highest impact with strongly negative sentiment, confirmed
by a second outlet (app/trading/events.confirmed_triggers). What happens next depends on
SHOCK_MODE:

* ``claude``: call A asks Claude whether the trigger is a new shock, a new development of a
  known one, or a repeat of something the market already processed. New or development: a
  pause starts for the affected coins (all markets when the scope is the market). It lasts at
  least SHOCK_MIN_HOURS and at most SHOCK_MAX_HOURS; from the minimum on, call B asks Claude
  every so often whether the market is still in shock, and the pause ends when it is not.
  Repeat: no pause.
* ``fixed``: a pause of SHOCK_PAUSE_HOURS from the trigger article, without Claude. The same
  pause is the fallback when Claude cannot judge call A (no key, the budget is spent, an error).

Per event key at most one pause runs at a time; articles about the same fact during a pause
only feed the news flow that call B sees. Every judgement is journaled as a decision, so the
journal says why buying stopped and started again. Selling and the exits are never paused.

The bounds follow the current settings, so a change applies to running pauses as well.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.budget import BudgetExceeded
from app.ai.shock_assessor import OngoingVerdict, TriggerVerdict, untrusted_text
from app.db import Decision, NewsEvent, NewsItem, Position, ShockPause, clip
from app.news.registry import MARKET_WIDE, members_of, merge_coins, related_events, upsert_event
from app.news.service import (
    event_key_for,
    load_all_etf_flows,
    load_market_mood,
    load_market_mood_previous,
)
from app.news.sources import aware
from app.trading.events import confirmed_triggers, upcoming_events
from app.trading.history import SHOCK_SIGNAL
from app.trading.shock_metrics import abnormality, since_first_seen

if TYPE_CHECKING:
    from app.trading.engine import TradingEngine

log = logging.getLogger(__name__)

# Articles older than this never trigger anything: they are not news any more.
TRIGGER_WINDOW_H = 24
HISTORY_DAYS = 30  # the shock list on the trading page and in the status
MIN_HOURS_RANGE = (0.25, 12.0)
MAX_HOURS_RANGE = (1.0, 72.0)
RECHECK_RANGE = (15, 240)
CALL_TIMEOUT_S = 120.0  # one assessment, including the one retry on an invalid answer
CANDLES_MIN, CANDLES_MAX = 200, 1440
NEWS_FLOW_HOURS = 6
LATEST_ARTICLES = 5
UPCOMING_HOURS = 3
REFERENCE_MARKET = "BTC-EUR"
SUMMARY_MAX = 400
# Journal labels per verdict.
LABEL_A = {"nieuw": "nieuw", "ontwikkeling": "ontwikkeling", "herhaling": "herhaling"}


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(max(number, low), high)


def _hm(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%H:%M")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _hours_text(hours: float) -> str:
    text = f"{hours:g}".replace(".", ",")
    return f"{text} uur"


def _duration_text(delta: timedelta) -> str:
    """'45 min', '2 uur' or '2 uur 15 min'."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes} min"
    return f"{hours} uur" if minutes == 0 else f"{hours} uur {minutes} min"


def news_direction(items: list[NewsItem], now: datetime) -> str:
    """escaleert / stabiel / kalmeert, from the sentiment of the last three hours against the
    three before (one vote per article)."""

    def window(lo: int, hi: int) -> list[float]:
        return [
            it.sentiment
            for it in items
            if it.sentiment is not None
            and now - timedelta(hours=hi) < aware(it.published_at) <= now - timedelta(hours=lo)
        ]

    recent, older = window(0, 3), window(3, 6)
    if not recent:
        return "kalmeert"
    mean_recent = statistics.fmean(recent)
    if not older:
        return "escaleert" if mean_recent <= -0.5 else "stabiel"
    diff = mean_recent - statistics.fmean(older)
    if diff <= -0.15 or (len(recent) >= 2 * len(older) and diff < 0.15 and mean_recent < -0.3):
        return "escaleert"
    if diff >= 0.15 or 2 * len(recent) <= len(older):
        return "kalmeert"
    return "stabiel"


class ShockGuard:
    """Runs the shock pauses of one TradingEngine; reads its settings and clients."""

    def __init__(self, engine: TradingEngine):
        self.engine = engine
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_run: datetime | None = None

    # -- settings -------------------------------------------------------------------------

    @property
    def min_hours(self) -> float:
        return _clamp(self.engine.shock_min_hours, *MIN_HOURS_RANGE, default=1.0)

    @property
    def max_hours(self) -> float:
        return max(
            _clamp(self.engine.shock_max_hours, *MAX_HOURS_RANGE, default=48.0), self.min_hours
        )

    @property
    def recheck_min(self) -> int:
        return int(_clamp(self.engine.shock_recheck_min, *RECHECK_RANGE, default=60))

    @property
    def fixed_hours(self) -> float:
        return _clamp(self.engine.shock_pause_hours, 0.0, 72.0, default=2.0)

    def tracked(self) -> list[str]:
        return [m.split("-")[0] for m in self.engine.markets]

    def _claude_ready(self, session: Session) -> str | None:
        """Why Claude cannot judge now (behave as the fixed mode), or None when it can."""
        if self.engine.shock_mode != "claude":
            return "schokbeoordeling staat op vast"
        assessor = self.engine.shock_assessor
        if assessor is None or not assessor.available:
            return "geen Claude-sleutel"
        try:
            if assessor.budget.status(session)["exhausted"]:
                return "het Claude-budget is op"
        except Exception:  # noqa: BLE001 - an unreadable budget: let the call itself decide
            session.rollback()
        return None

    # -- pauses ---------------------------------------------------------------------------

    def bounds(self, pause: ShockPause) -> dict[str, datetime | None]:
        """When the pause may end at the earliest, must end at the latest, and when it is
        looked at next. ``until``: the earliest moment buying can resume."""
        start = aware(pause.started_at)
        if pause.mode == "fixed":
            end = start + timedelta(hours=self.fixed_hours)
            return {"min_until": end, "max_until": end, "next_recheck_at": None, "until": end}
        min_until = start + timedelta(hours=self.min_hours)
        max_until = start + timedelta(hours=self.max_hours)
        nxt = aware(pause.next_recheck_at) if pause.next_recheck_at else min_until
        nxt = min(max(nxt, min_until), max_until)
        return {
            "min_until": min_until,
            "max_until": max_until,
            "next_recheck_at": nxt,
            "until": nxt,
        }

    def is_active(self, pause: ShockPause, now: datetime) -> bool:
        if pause.ended_at is not None or aware(pause.started_at) > now:
            return False
        return now < self.bounds(pause)["max_until"]

    def active_pauses(self, session: Session, now: datetime) -> list[ShockPause]:
        rows = session.scalars(
            select(ShockPause)
            .where(ShockPause.ended_at.is_(None))
            .order_by(ShockPause.started_at.desc(), ShockPause.id.desc())
        ).all()
        return [p for p in rows if self.is_active(p, now)]

    def _covers(self, pause: ShockPause, coin: str) -> bool:
        coins = pause.coin_list()
        return pause.scope == "markt" or MARKET_WIDE in coins or coin in coins

    def pause_for(self, session: Session, coin: str, now: datetime) -> ShockPause | None:
        """The running pause that keeps ``coin`` from being bought, or None."""
        if not self.engine.risk_off_enabled:
            return None
        for pause in self.active_pauses(session, now):
            if self._covers(pause, coin):
                return pause
        return None

    def reason_nl(self, pause: ShockPause) -> str:
        b = self.bounds(pause)
        start = aware(pause.started_at)
        if pause.mode == "fixed":
            return (
                f"nieuwsschok {pause.event_key} sinds {_hm(start)} UTC; vaste pauze, geen nieuwe "
                f"aankopen tot {_hm(b['until'])} UTC"
            )
        return (
            f"nieuwsschok {pause.event_key} sinds {_hm(start)} UTC; volgende beoordeling "
            f"{_hm(b['next_recheck_at'])} UTC, uiterlijk {_hm(b['max_until'])} UTC"
        )

    def risk_off_info(self, pause: ShockPause) -> dict[str, Any]:
        """The risk-off entry the engine, the pages and Claude's decision context see."""
        b = self.bounds(pause)
        return {
            "kind": "shock",
            "event_key": pause.event_key,
            "label": pause.summary_nl or pause.event_key,
            "mode": pause.mode,
            "scope": pause.scope,
            "at": _iso(aware(pause.started_at)),
            "until": _iso(b["until"]),
            "min_until": _iso(b["min_until"]),
            "max_until": _iso(b["max_until"]),
            "next_recheck_at": _iso(b["next_recheck_at"]),
            "reason_nl": self.reason_nl(pause),
        }

    def describe(self, pause: ShockPause) -> dict[str, Any]:
        """One pause for the status: what, since when, its bounds and the last judgement."""
        b = self.bounds(pause)
        verdicts = pause.verdict_list()
        last = verdicts[-1] if verdicts else None
        return {
            "event_key": pause.event_key,
            "summary": pause.summary_nl,
            "mode": pause.mode,
            "scope": pause.scope,
            "coins": pause.coin_list(),
            "shock_started_at": _iso(aware(pause.started_at)),
            "min_until": _iso(b["min_until"]),
            "max_until": _iso(b["max_until"]),
            "next_recheck_at": _iso(b["next_recheck_at"]),
            "reason_nl": self.reason_nl(pause),
            "last_verdict": None
            if last is None
            else {
                "at": last.get("at"),
                "kind": last.get("kind"),
                "verdict": last.get("verdict"),
                "confidence": last.get("confidence"),
                "reason": last.get("reason"),
            },
        }

    def status(self, session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        if not self.engine.risk_off_enabled:
            return []
        return [self.describe(p) for p in self.active_pauses(session, now)]

    def history(
        self, session: Session, now: datetime | None = None, days: int = HISTORY_DAYS
    ) -> list[dict[str, Any]]:
        """The pauses of the last ``days`` days, newest first: what, when, how long and how
        they ended. A running pause is in it too, with its duration so far."""
        now = now or datetime.now(UTC)
        rows = session.scalars(
            select(ShockPause)
            .where(ShockPause.started_at >= now - timedelta(days=days))
            .order_by(ShockPause.started_at.desc(), ShockPause.id.desc())
        ).all()
        out = []
        for pause in rows:
            start = aware(pause.started_at)
            if start > now:
                continue
            active = pause.ended_at is None and self.is_active(pause, now)
            end = aware(pause.ended_at) if pause.ended_at else None
            if end is None and not active:  # ran out, not yet marked by the next tick
                end = self.bounds(pause)["max_until"]
            until = end or now
            verdicts = pause.verdict_list()
            out.append(
                {
                    "event_key": pause.event_key,
                    "summary": pause.summary_nl,
                    "mode": pause.mode,
                    "scope": pause.scope,
                    "coins": pause.coin_list(),
                    "coins_nl": "hele markt"
                    if pause.scope == "markt"
                    else ", ".join(pause.coin_list()),
                    "started_at": _iso(start),
                    "ended_at": _iso(end),
                    "active": active,
                    "hours": round((until - start).total_seconds() / 3600, 2),
                    "duration_nl": _duration_text(until - start),
                    "end_reason": None if active else (pause.end_reason or "maximum"),
                    "ended_nl": self._ended_text(pause, active),
                    "assessments": len(verdicts),
                    "last_reason": verdicts[-1].get("reason") if verdicts else None,
                }
            )
        return out

    def _ended_text(self, pause: ShockPause, active: bool) -> str:
        if active:
            return "loopt nog"
        reason = pause.end_reason or "maximum"
        if reason == "claude":
            return "Claude zag de schok als voorbij"
        if reason == "vast" or pause.mode == "fixed":
            return f"vaste pauze van {_hours_text(self.fixed_hours)} om"
        return f"maximum van {_hours_text(self.max_hours)} bereikt"

    def pause_markets(self, pause: ShockPause) -> list[str]:
        return [m for m in self.engine.markets if self._covers(pause, m.split("-")[0])]

    # -- the work -------------------------------------------------------------------------

    async def tick(self, session: Session, now: datetime | None = None, rechecks: bool = True):
        """Close what ran out, judge new triggers (call A) and, with ``rechecks``, the pauses
        whose recheck is due (call B). One at a time: the news job, the cycle and the
        scheduler may all call this."""
        async with self._lock:
            now = now or datetime.now(UTC)
            cache: dict[str, list | None] = {}
            await self._expire(session, now)
            if not self.engine.risk_off_enabled:
                return
            await self._scan(session, now, cache)
            if rechecks:
                await self._recheck_due(session, now, cache)
            self.last_run = now

    async def on_news(self, session: Session, items: list[NewsItem]) -> None:
        """After a news refresh: judge new triggers right away (no rechecks)."""
        await self.tick(session, rechecks=False)

    async def _expire(self, session: Session, now: datetime) -> None:
        """Mark pauses that ran out as ended; a Claude pause at its maximum is journaled."""
        rows = session.scalars(select(ShockPause).where(ShockPause.ended_at.is_(None))).all()
        for pause in rows:
            if aware(pause.started_at) > now or self.is_active(pause, now):
                continue
            end = self.bounds(pause)["max_until"]
            pause.ended_at = min(end, now)
            pause.end_reason = "vast" if pause.mode == "fixed" else "maximum"
            event = session.get(NewsEvent, pause.event_key)
            if event is not None:
                event.last_shock_ended_at = pause.ended_at
            if pause.mode == "claude":
                await self._journal(
                    session,
                    self.pause_markets(pause),
                    now,
                    source="rules",
                    reasoning=(
                        f"Schokpauze {pause.event_key} voorbij: het maximum van "
                        f"{_hours_text(self.max_hours)} is bereikt. Kopen kan weer."
                    ),
                    blocked=False,
                )
        session.commit()

    async def _scan(self, session: Session, now: datetime, cache: dict) -> None:
        tracked = self.tracked()
        triggers = confirmed_triggers(session, now, TRIGGER_WINDOW_H, tracked)
        by_key: dict[str, list[NewsItem]] = {}
        for it in triggers:
            by_key.setdefault(event_key_for(it)[:64], []).append(it)
        for key, items in by_key.items():
            newest_id = max(it.id for it in items)
            members = members_of(session, key) if items[0].event_key else list(items)
            event = upsert_event(
                session,
                key,
                members or items,
                prices=self.engine.last_prices,
                tracked=tracked,
                now=now,
            )
            session.commit()
            if event is None or newest_id <= (event.last_trigger_id or 0):
                continue
            if any(p.event_key == key for p in self.active_pauses(session, now)):
                # A pause for this fact runs already: no second call A (the articles feed B).
                event.last_trigger_id = newest_id
                session.commit()
                continue
            lead = max(items, key=lambda it: (aware(it.published_at), it.id))
            try:
                await self._judge(session, event, lead, items, members or items, now, cache)
            except Exception:  # noqa: BLE001 - one fact must not stop the others
                session.rollback()
                log.exception("shock judgement for %s failed", key)
                event = session.get(NewsEvent, key)
                if event is None:
                    continue
            event.last_trigger_id = newest_id
            session.commit()

    # -- call A ---------------------------------------------------------------------------

    def _trigger_coins(self, items: list[NewsItem]) -> list[str]:
        coins = merge_coins(*(it.coin_list() for it in items))
        return [MARKET_WIDE] if coins == MARKET_WIDE else [c for c in coins.split(",") if c]

    async def _judge(
        self,
        session: Session,
        event: NewsEvent,
        lead: NewsItem,
        triggers: list[NewsItem],
        members: list[NewsItem],
        now: datetime,
        cache: dict,
    ) -> None:
        coins = self._trigger_coins(triggers)
        why_not = self._claude_ready(session)
        if why_not is not None:
            await self._start_fixed(session, event, lead, coins, now, why_not)
            return
        facts = await self._facts_a(session, event, lead, members, coins, now, cache)
        assessor = self.engine.shock_assessor
        try:
            verdict: TriggerVerdict = await asyncio.wait_for(
                assessor.assess_trigger(session, facts, self.tracked()), CALL_TIMEOUT_S
            )
        except BudgetExceeded as exc:
            session.rollback()
            await self._start_fixed(session, event, lead, coins, now, "het Claude-budget is op")
            log.warning("shock assessment A: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001 - API error, time-out, invalid answer twice
            session.rollback()
            self.last_error = f"schokbeoordeling mislukt: {type(exc).__name__}"
            log.warning("shock assessment A for %s failed: %s", event.event_key, exc)
            why = "time-out" if isinstance(exc, TimeoutError) else "fout bij Claude"
            await self._start_fixed(
                session, event, lead, coins, now, f"schokbeoordeling mislukt ({why})"
            )
            return
        self.last_error = None
        record = {
            "at": _iso(now),
            "kind": "A",
            "verdict": verdict.verdict,
            "confidence": round(verdict.confidence, 2),
            "reason": verdict.reason,
        }
        label = f"Schokbeoordeling ({LABEL_A[verdict.verdict]}): {verdict.reason}"
        model = assessor.model
        if not verdict.pause:
            markets = self._markets_for(coins)
            await self._journal(
                session,
                markets,
                now,
                source="claude",
                model=model,
                confidence=verdict.confidence,
                reasoning=f"{label} Geen schokpauze; kopen blijft mogelijk.",
                blocked=False,
            )
            session.commit()
            return
        scope = verdict.scope
        pause_coins = merge_coins(verdict.affected_coins or coins)
        if scope == "markt" or pause_coins in (MARKET_WIDE, ""):
            scope, pause_coins = "markt", MARKET_WIDE
        pause = ShockPause(
            event_key=event.event_key,
            mode="claude",
            scope=scope,
            coins=pause_coins,
            started_at=now,
            next_recheck_at=now + timedelta(hours=self.min_hours),
            trigger_news_id=lead.id,
            summary_nl=clip(lead.summary_nl or lead.title, SUMMARY_MAX),
            verdicts=json.dumps([record], ensure_ascii=False),
            failures=0,
        )
        session.add(pause)
        event.shock_count = (event.shock_count or 0) + 1
        event.last_shock_started_at = now
        session.flush()
        await self._journal(
            session,
            self.pause_markets(pause),
            now,
            source="claude",
            model=model,
            confidence=verdict.confidence,
            reasoning=f"{label} Geen nieuwe aankopen; {self._next_text(pause)}.",
            blocked=True,
        )
        session.commit()
        log.info("shock pause %s started (%s, %s)", event.event_key, verdict.verdict, scope)

    def _next_text(self, pause: ShockPause) -> str:
        b = self.bounds(pause)
        return (
            f"volgende beoordeling {_hm(b['next_recheck_at'])} UTC, uiterlijk "
            f"{_hm(b['max_until'])} UTC"
        )

    def _markets_for(self, coins: list[str]) -> list[str]:
        if MARKET_WIDE in coins:
            return list(self.engine.markets)
        return [m for m in self.engine.markets if m.split("-")[0] in coins]

    async def _start_fixed(
        self,
        session: Session,
        event: NewsEvent,
        lead: NewsItem,
        coins: list[str],
        now: datetime,
        why: str,
    ) -> None:
        """The fixed pause: SHOCK_PAUSE_HOURS from the trigger article (the fixed mode, or the
        fallback when Claude cannot judge). 0 hours: no pause."""
        hours = self.fixed_hours
        start = min(aware(lead.published_at), now)
        if hours <= 0 and self.engine.shock_mode == "claude":
            await self._journal(
                session,
                self._markets_for(coins),
                now,
                source="rules",
                reasoning=(
                    f"Schokbeoordeling niet mogelijk: {why}. De vaste schok-pauze staat op 0 uur, "
                    f"dus geen pauze voor nieuwsschok {event.event_key}."
                ),
                blocked=False,
            )
            session.commit()
            return
        if hours <= 0 or start + timedelta(hours=hours) <= now:
            return
        scope, pause_coins = (
            ("markt", MARKET_WIDE)
            if MARKET_WIDE in coins
            else (
                "coin",
                merge_coins(coins),
            )
        )
        pause = ShockPause(
            event_key=event.event_key,
            mode="fixed",
            scope=scope,
            coins=pause_coins,
            started_at=start,
            next_recheck_at=None,
            trigger_news_id=lead.id,
            summary_nl=clip(lead.summary_nl or lead.title, SUMMARY_MAX),
            verdicts=json.dumps(
                [
                    {
                        "at": _iso(now),
                        "kind": "vast",
                        "verdict": "vast",
                        "confidence": None,
                        "reason": why,
                    }
                ],
                ensure_ascii=False,
            ),
            failures=0,
        )
        session.add(pause)
        event.shock_count = (event.shock_count or 0) + 1
        event.last_shock_started_at = start
        session.flush()
        until = self.bounds(pause)["until"]
        intro = (
            "Schok-pauze (vast)"
            if self.engine.shock_mode != "claude"
            else f"Schokbeoordeling niet mogelijk: {why}. Terugval op de vaste schok-pauze"
        )
        await self._journal(
            session,
            self.pause_markets(pause),
            now,
            source="rules",
            reasoning=(
                f"{intro} van {_hours_text(hours)}: nieuwsschok {event.event_key}; geen nieuwe "
                f"aankopen tot {_hm(until)} UTC."
            ),
            blocked=True,
        )
        session.commit()
        log.info("fixed shock pause %s started (%s)", event.event_key, why)

    # -- call B ---------------------------------------------------------------------------

    async def _recheck_due(self, session: Session, now: datetime, cache: dict) -> None:
        for pause in self.active_pauses(session, now):
            if pause.mode != "claude":
                continue
            b = self.bounds(pause)
            if now < b["min_until"] or now < b["next_recheck_at"]:
                continue
            try:
                await self._recheck(session, pause, now, cache)
            except Exception:  # noqa: BLE001 - one pause must not stop the others
                session.rollback()
                log.exception("shock recheck for %s failed", pause.event_key)

    async def _recheck(
        self, session: Session, pause: ShockPause, now: datetime, cache: dict
    ) -> None:
        b = self.bounds(pause)
        why_not = self._claude_ready(session)
        if why_not is not None:
            await self._as_fixed(session, pause, now, why_not)
            return
        facts = await self._facts_b(session, pause, now, cache)
        assessor = self.engine.shock_assessor
        try:
            verdict: OngoingVerdict = await asyncio.wait_for(
                assessor.assess_ongoing(session, facts), CALL_TIMEOUT_S
            )
        except BudgetExceeded as exc:
            session.rollback()
            log.warning("shock assessment B: %s", exc)
            await self._as_fixed(session, pause, now, "het Claude-budget is op")
            return
        except Exception as exc:  # noqa: BLE001 - keep the pause, try again later
            session.rollback()
            self.last_error = f"herbeoordeling schokpauze mislukt: {type(exc).__name__}"
            log.warning("shock assessment B for %s failed: %s", pause.event_key, exc)
            pause.failures = (pause.failures or 0) + 1
            pause.next_recheck_at = min(now + timedelta(minutes=self.recheck_min), b["max_until"])
            if pause.failures == 1:
                await self._journal(
                    session,
                    self.pause_markets(pause),
                    now,
                    source="rules",
                    reasoning=(
                        f"Herbeoordeling van schokpauze {pause.event_key} mislukt; de pauze "
                        f"blijft staan. {self._next_text(pause)[0].upper()}"
                        f"{self._next_text(pause)[1:]}."
                    ),
                    blocked=True,
                )
            session.commit()
            return
        self.last_error = None
        verdicts = pause.verdict_list()
        verdicts.append(
            {
                "at": _iso(now),
                "kind": "B",
                "verdict": "actief" if verdict.shock_active else "voorbij",
                "confidence": round(verdict.confidence, 2),
                "reason": verdict.reason,
            }
        )
        pause.verdicts = json.dumps(verdicts, ensure_ascii=False)
        pause.failures = 0
        markets = self.pause_markets(pause)
        model = assessor.model
        if verdict.shock_active:
            pause.next_recheck_at = min(
                now + timedelta(minutes=verdict.recheck_in_minutes), b["max_until"]
            )
            await self._journal(
                session,
                markets,
                now,
                source="claude",
                model=model,
                confidence=verdict.confidence,
                reasoning=(
                    f"Schokbeoordeling (actief): {verdict.reason} Geen nieuwe aankopen; "
                    f"{self._next_text(pause)}."
                ),
                blocked=True,
            )
        else:
            self._end(session, pause, now, "claude")
            await self._journal(
                session,
                markets,
                now,
                source="claude",
                model=model,
                confidence=verdict.confidence,
                reasoning=f"Schokbeoordeling (voorbij): {verdict.reason} Kopen kan weer.",
                blocked=False,
            )
        session.commit()

    def _end(self, session: Session, pause: ShockPause, now: datetime, reason: str) -> None:
        pause.ended_at = now
        pause.end_reason = reason
        event = session.get(NewsEvent, pause.event_key)
        if event is not None:
            event.last_shock_ended_at = now

    async def _as_fixed(self, session: Session, pause: ShockPause, now: datetime, why: str):
        """Claude cannot recheck (fixed mode, no key, budget spent): the pause behaves as a
        fixed one, ending SHOCK_PAUSE_HOURS after its start, within its own bounds."""
        b = self.bounds(pause)
        end = aware(pause.started_at) + timedelta(hours=self.fixed_hours)
        end = min(max(end, b["min_until"]), b["max_until"])
        if now >= end:
            self._end(session, pause, now, "vast")
            await self._journal(
                session,
                self.pause_markets(pause),
                now,
                source="rules",
                reasoning=(
                    f"Schokpauze {pause.event_key} voorbij zonder herbeoordeling ({why}): de "
                    f"vaste duur van {_hours_text(self.fixed_hours)} is om. Kopen kan weer."
                ),
                blocked=False,
            )
        else:
            pause.next_recheck_at = end
        session.commit()

    # -- facts ----------------------------------------------------------------------------

    async def _candles(self, market: str, now: datetime, since: datetime | None, cache: dict):
        hours = 0 if since is None else int((now - since).total_seconds() // 3600)
        limit = int(min(max(hours + 172, CANDLES_MIN), CANDLES_MAX))
        if market in cache and (cache[market] is None or len(cache[market]) >= limit):
            return cache[market]
        try:
            rows = await asyncio.wait_for(
                self.engine.client.candles(market, "1h", limit), self.engine.data_timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - a missing number is "onbekend", not a failure
            log.warning("shock facts: candles for %s failed: %s", market, exc)
            rows = None
        cache[market] = rows
        return rows

    def _price_now(self, market: str) -> float | None:
        price = self.engine.last_prices.get(market)
        return float(price) if price else None

    async def _since(
        self, coins: list[str], event: NewsEvent, now: datetime, cache: dict
    ) -> dict[str, dict]:
        first = aware(event.first_seen_at)
        stored = event.prices()
        out = {}
        for coin in coins:
            market = f"{coin}-EUR"
            rows = await self._candles(market, now, first, cache)
            out[coin] = since_first_seen(
                rows or [], now, first, stored.get(coin), self._price_now(market)
            )
        return out

    def _coins_for_facts(self, coins: list[str]) -> list[str]:
        tracked = self.tracked()
        if MARKET_WIDE in coins:
            return tracked
        return [c for c in coins if c in tracked] or tracked

    async def _facts_a(
        self,
        session: Session,
        event: NewsEvent,
        lead: NewsItem,
        members: list[NewsItem],
        coins: list[str],
        now: datetime,
        cache: dict,
    ) -> dict[str, Any]:
        published = aware(lead.published_at)
        prior = [m for m in members if m.id != lead.id and aware(m.published_at) <= published]
        first = aware(event.first_seen_at)
        related = related_events(session, event.event_key, now, ignore=self.tracked())
        btc = abnormality(await self._candles(REFERENCE_MARKET, now, None, cache) or [], now)
        facts: dict[str, Any] = {
            "event_key": event.event_key,
            "published_at": published,
            "minutes_since_published": max(int((now - published).total_seconds() // 60), 0),
            "coins": coins,
            "first_seen_at": first,
            "hours_since_first_seen": round((now - first).total_seconds() / 3600, 1),
            "prior_article_count": len(prior),
            "prior_shock_count": event.shock_count or 0,
            "last_shock_end_at": aware(event.last_shock_ended_at)
            if event.last_shock_ended_at
            else None,
            "related": [
                {
                    "event_key": r.event_key,
                    "first_seen_at": aware(r.first_seen_at),
                    "article_count": r.article_count,
                    "max_impact": r.max_impact,
                }
                for r in related
            ],
            "since": {},
            "btc_move_ratio": btc["move"][0],
            "btc_volume_ratio": btc["volume"][0],
            "fear_greed": load_market_mood(session, now)
            if self.engine.market_mood_enabled
            else None,
            # Net flows of the US spot ETFs of the coins this news is about (day figures).
            "etf_flows": load_all_etf_flows(session, self._coins_for_facts(coins), now)
            if self.engine.etf_flows_enabled
            else {},
            "untrusted": {
                "bericht": {
                    "titel": untrusted_text(lead.title),
                    "bron": untrusted_text(lead.source_name),
                    "samenvatting": untrusted_text(lead.summary_nl),
                }
            },
        }
        if prior:
            facts["since"] = await self._since(self._coins_for_facts(coins), event, now, cache)
        return facts

    async def _facts_b(
        self, session: Session, pause: ShockPause, now: datetime, cache: dict
    ) -> dict[str, Any]:
        event = session.get(NewsEvent, pause.event_key)
        coins = self._coins_for_facts(pause.coin_list() or [MARKET_WIDE])
        if pause.scope == "markt" and "BTC" not in coins:
            coins = ["BTC", *coins]
        first = aware(event.first_seen_at) if event else aware(pause.started_at)
        since: dict[str, dict] = {}
        abnormal: dict[str, dict] = {}
        for coin in coins:
            market = f"{coin}-EUR"
            rows = await self._candles(market, now, first, cache) or []
            stored = event.prices() if event else {}
            since[coin] = since_first_seen(
                rows, now, first, stored.get(coin), self._price_now(market)
            )
            abnormal[coin] = abnormality(rows, now)
        flow = self._news_flow(session, pause.event_key, now)
        per_hour = [
            sum(
                1
                for it in flow
                if now - timedelta(hours=h + 1) < aware(it.published_at) <= now - timedelta(hours=h)
            )
            for h in range(NEWS_FLOW_HOURS)
        ]
        latest = sorted(flow, key=lambda it: (aware(it.published_at), it.id), reverse=True)
        latest = latest[:LATEST_ARTICLES]
        verdicts = pause.verdict_list()
        start = aware(pause.started_at)
        return {
            "event_key": pause.event_key,
            "scope": pause.scope,
            "affected_coins": [MARKET_WIDE] if pause.scope == "markt" else pause.coin_list(),
            "first_seen_at": first,
            "hours_since_first_seen": round((now - first).total_seconds() / 3600, 1),
            "shock_started_at": start,
            "min_hours": self.min_hours,
            "max_hours": self.max_hours,
            "since": since,
            "abnormality": abnormal,
            "articles_per_hour": per_hour,
            "latest_articles": [
                {
                    "ref": f"n{i}",
                    "published_at": aware(it.published_at),
                    "impact": it.impact,
                    "sentiment": it.sentiment,
                }
                for i, it in enumerate(latest, 1)
            ],
            "news_direction": news_direction(flow, now),
            "fear_greed": load_market_mood(session, now)
            if self.engine.market_mood_enabled
            else None,
            "fear_greed_yesterday": load_market_mood_previous(session, now)
            if self.engine.market_mood_enabled
            else None,
            "etf_flows": load_all_etf_flows(session, coins, now)
            if self.engine.etf_flows_enabled
            else {},
            "upcoming_events": [
                {"label": e.label, "at": e.at}
                for e in upcoming_events(
                    self.engine.calendar, now, limit=5, horizon_hours=UPCOMING_HOURS
                )
            ],
            "previous_verdicts": [
                {
                    "ref": f"v{i}",
                    "at": v.get("at"),
                    "label": v.get("verdict")
                    if v.get("kind") == "B"
                    else f"start: {v.get('verdict')}",
                    "confidence": v.get("confidence"),
                }
                for i, v in enumerate(verdicts, 1)
            ],
            "untrusted": {
                "oorspronkelijke_samenvatting": untrusted_text(
                    (event.first_summary_nl if event else None) or pause.summary_nl
                ),
                "berichten": [
                    {
                        "ref": f"n{i}",
                        "titel": untrusted_text(it.title),
                        "samenvatting": untrusted_text(it.summary_nl),
                    }
                    for i, it in enumerate(latest, 1)
                ],
                "oordelen": [
                    {"ref": f"v{i}", "reden": untrusted_text(v.get("reason"))}
                    for i, v in enumerate(verdicts, 1)
                ],
            },
        }

    def _news_flow(self, session: Session, key: str, now: datetime) -> list[NewsItem]:
        """The assessed, relevant articles about this fact of the last NEWS_FLOW_HOURS."""
        rows = session.scalars(
            select(NewsItem).where(
                NewsItem.assessed.is_(True),
                NewsItem.relevant.is_(True),
                NewsItem.published_at > now - timedelta(hours=NEWS_FLOW_HOURS),
                NewsItem.published_at <= now,
            )
        ).all()
        return [it for it in rows if event_key_for(it)[:64] == key]

    # -- journal --------------------------------------------------------------------------

    async def _journal(
        self,
        session: Session,
        markets: list[str],
        now: datetime,
        *,
        source: str,
        reasoning: str,
        blocked: bool,
        model: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """One decision per market (no commit), so each market's journal says why buying
        stopped or started. A market without a known price is skipped (a decision needs one)."""
        prices = dict(self.engine.last_prices)
        missing = [m for m in markets if not prices.get(m)]
        if missing:
            try:
                fetched = await asyncio.wait_for(
                    self.engine.client.ticker_prices(missing), self.engine.data_timeout_s
                )
                prices.update({m: p for m, p in (fetched or {}).items() if p})
            except Exception as exc:  # noqa: BLE001
                log.warning("shock journal: prices for %s failed: %s", missing, exc)
        mode = self.engine.mode
        for market in markets:
            price = prices.get(market)
            if not price:
                log.warning("shock journal: no price for %s, entry skipped", market)
                continue
            has_position = (
                session.scalar(
                    select(Position.id).where(
                        Position.mode == mode, Position.market == market, Position.status == "open"
                    )
                )
                is not None
            )
            session.add(
                Decision(
                    created_at=now,
                    mode=mode,
                    market=market,
                    price=float(price),
                    signal=SHOCK_SIGNAL,
                    has_position=has_position,
                    source=source,
                    model=model,
                    action="hold",
                    confidence=confidence,
                    reasoning=reasoning,
                    blocked_by="risk-off" if blocked else None,
                )
            )
