"""One honest status of the bot for the pages, and the risk limits as current figures.

:func:`bot_health` answers "does the bot run, and can it buy?" from the engine's state: the
kill switch, TRADING_ENABLED, Claude (with CLAUDE_UNAVAILABLE_MODE), the daily limits, the last
cycle and the exit loop that guards stop-loss and take-profit. :func:`risk_now` gives the
numbers behind the limit meters (exposure, today's result, orders today).

Both only read the database and what the engine already knows: no Bitvavo calls, so a page
view or a poll of /api/status stays fast while Bitvavo is down. /health stays a pure liveness
check and never looks at any of this.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.bitvavo.broker import PAPER_WALLET_KEY
from app.db import Setting
from app.labels import KILL_SWITCH_HELP_NL, orders_limit_nl
from app.trading import risk
from app.trading.events import active_event

# What the kill switch does: the same sentence as next to the kill switch on every page
# (app/labels.py; checked against TradingEngine: risk.check_buy refuses every buy, the exit
# loop and strategy sell signals are not affected).
PAUSED_DETAIL = KILL_SWITCH_HELP_NL
OFF_DETAIL = (
    "Automatisch handelen staat uit; je kunt zelf een cyclus starten en de bewaking van "
    "stop-loss en take-profit loopt gewoon door."
)
LEVELS = ("ok", "warn", "error", "paused", "off")
# Right after a (re)start the loops have not had their first run yet: no alarm in this time.
START_GRACE = timedelta(minutes=2)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hm(value: datetime) -> str:
    return f"{value.astimezone(UTC):%H:%M}"


def _num(value: float, decimals: int = 2) -> str:
    """Dutch number without trailing zeros: 5 -> '5', 2.5 -> '2,5', 0.04 -> '0,04'."""
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if text not in ("-0", "") else "0"


def describe_problem(exc: BaseException) -> str:
    """What went wrong, in Dutch for the pages; the raw error belongs in the log."""
    from app.bitvavo.client import BitvavoError

    if isinstance(exc, BitvavoError):
        if exc.status == 0:
            return "Bitvavo reageert niet (geen verbinding of een time-out)"
        if exc.rate_limited:
            return "Bitvavo laat tijdelijk geen verzoeken meer toe (limiet bereikt)"
        if exc.status >= 500:
            return f"Bitvavo heeft een storing (fout {exc.status})"
        return f"Bitvavo weigerde het verzoek (fout {exc.status})"
    if isinstance(exc, TimeoutError):
        return "geen antwoord binnen de tijdslimiet"
    return f"onverwachte fout ({type(exc).__name__})"


def next_utc_midnight(now: datetime) -> datetime:
    """When the daily limits (orders, loss) start again: the next 00:00 UTC."""
    now = now.astimezone(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _paper_wallet(session: Session, start_eur: float) -> dict[str, float]:
    """The paper balances, read-only (PaperBroker creates the row on first use)."""
    row = session.get(Setting, PAPER_WALLET_KEY)
    if row is None:
        return {"EUR": float(start_eur)}
    try:
        return {k: float(v) for k, v in json.loads(row.value).items()}
    except (ValueError, TypeError, AttributeError):
        return {}


def risk_now(
    engine: Any,
    session: Session,
    settings: Any,
    *,
    balances: dict[str, float] | None = None,
    prices: dict[str, float] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The hard limits as they stand now, computed like ``risk.check_buy`` does.

    ``balances`` and ``prices`` are what the caller already fetched (the overview); without
    them the engine's last known prices and portfolio are used. ``portfolio_eur`` and the
    percentages are None when the portfolio value is not known (live, before the first cycle).
    """
    now = now or datetime.now(UTC)
    mode = engine.mode
    limits = engine.limits
    known_prices = dict(engine.last_prices or {})
    if prices:
        known_prices.update(prices)
    orders_today = risk.trades_today(session, mode, now)
    realized = risk.realized_pnl_today(session, mode, now)
    unrealized = risk.unrealized_pnl(session, mode, known_prices)
    total: float | None = None
    exposure: float | None = None
    as_of: datetime | None = None
    if mode == "paper" and not balances:
        balances = _paper_wallet(session, settings.paper_start_eur)
    if balances:
        missing = [
            s
            for s, amount in balances.items()
            if s != "EUR" and amount > 0 and not known_prices.get(f"{s}-EUR")
        ]
        if mode == "paper" and missing:
            # No price (yet) for a coin: count the bot's positions at their last known or
            # entry value rather than leaving them out.
            exposure = risk.position_exposure(session, mode, known_prices)
            total = balances.get("EUR", 0.0) + exposure
        else:
            total, exposure = engine.portfolio_from(session, mode, balances, known_prices)
        as_of = now if prices else _aware(engine.last_prices_at)
    elif mode == "live" and engine.live_capital_eur > 0:
        total = engine.live_capital_eur
        exposure = risk.position_exposure(session, "live", known_prices)
        as_of = _aware(engine.last_prices_at)
    elif engine.last_portfolio and engine.last_portfolio.get("mode") == mode:
        total = engine.last_portfolio["total"]
        exposure = engine.last_portfolio["exposure"]
        as_of = _aware(engine.last_portfolio.get("at"))
    day_result = realized + min(unrealized, 0.0)
    known = total is not None and total > 0
    exposure_pct = exposure / total * 100 if known and exposure is not None else None
    room = total * limits.max_exposure_pct / 100 - (exposure or 0.0) if known else None
    return {
        "mode": mode,
        "as_of": as_of,
        "portfolio_eur": round(total, 2) if total is not None else None,
        "exposure_eur": round(exposure, 2) if exposure is not None else None,
        "exposure_pct": round(exposure_pct, 2) if exposure_pct is not None else None,
        "max_exposure_pct": limits.max_exposure_pct,
        "exposure_room_eur": round(max(room, 0.0), 2) if room is not None else None,
        "exposure_limit_reached": room is not None and room < limits.min_trade_eur,
        "realized_today_eur": round(realized, 2),
        "unrealized_eur": round(unrealized, 2),
        # What the daily loss limit counts: today's realized result plus open losses.
        "day_result_eur": round(day_result, 2),
        "day_result_pct": round(day_result / total * 100, 2) if known else None,
        "daily_loss_limit_pct": limits.daily_loss_limit_pct,
        "loss_limit_reached": bool(
            known and -day_result / total * 100 >= limits.daily_loss_limit_pct
        ),
        "orders_today": orders_today,
        "max_orders_per_day": limits.max_trades_per_day,
        "orders_limit_reached": orders_today >= limits.max_trades_per_day,
        "max_trade_eur": limits.max_trade_eur,
        "resets_at": next_utc_midnight(now),
    }


def bot_health(
    engine: Any,
    session: Session,
    settings: Any,
    *,
    paused: bool | None = None,
    budget: dict[str, Any] | None = None,
    scheduled_last: datetime | None = None,
    risk_figures: dict[str, Any] | None = None,
    strategy_invalid: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The bot's status for the hero of Overzicht and Handelen and for /api/status.

    Returns ``level`` ("ok", "warn", "error", "paused" or "off"), a short Dutch ``label``, one
    sentence ``detail`` (or None), every applicable ``reasons``, the cycle times
    (``last_cycle``, ``next_cycle``) and the exit loop (``exits_ok``, ``last_exit_ok``,
    ``exit_interval_min``), plus a few extra fields for the pages (see the notes).
    ``scheduled_last`` is when the scheduler last started a cycle (it plans the next one from
    that); ``budget`` is ``BudgetGuard.status()``; ``risk_figures`` is :func:`risk_now`.
    """
    now = now or datetime.now(UTC)
    if paused is None:
        paused = risk.is_paused(session)
    enabled = bool(settings.trading_enabled and settings.trade_interval_min > 0)
    interval = timedelta(minutes=max(settings.trade_interval_min, 1))
    exit_interval_min = settings.exit_check_interval_min
    rn = risk_figures if risk_figures is not None else risk_now(engine, session, settings, now=now)
    started = _aware(engine.started_at) or now
    warming_up = now - started <= START_GRACE

    errors: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []  # why the bot cannot buy right now
    notes: list[str] = []

    # -- the exit loop: stop-loss, trailing stop and take-profit --
    last_exit_ok = _aware(engine.last_exit_ok)
    failing_since = _aware(engine.exit_failing_since)
    exit_grace = max(timedelta(minutes=2 * exit_interval_min), START_GRACE)
    if failing_since is not None:
        exits_ok = False
        errors.append(
            f"De bewaking van stop-loss en take-profit lukt niet sinds {_hm(failing_since)} UTC: "
            f"{engine.exit_error or 'onbekende fout'}."
        )
    elif last_exit_ok is not None and now - last_exit_ok <= exit_grace:
        exits_ok = True
    elif now - started <= START_GRACE + timedelta(minutes=exit_interval_min):
        exits_ok = True  # just (re)started: the first run is still to come
    else:
        exits_ok = False
        since = f"sinds {_hm(last_exit_ok)} UTC" if last_exit_ok else "sinds de start"
        errors.append(
            f"De bewaking van stop-loss en take-profit is {since} niet meer gelukt; die hoort "
            f"elke {exit_interval_min} min te draaien."
        )
    unprotected = list(engine.unprotected or [])
    if unprotected and failing_since is None:
        n = len(unprotected)
        errors.append(
            f"{n} " + ("positie wordt" if n == 1 else "posities worden") + " niet bewaakt door "
            "stop-loss of take-profit."
        )

    # -- the trading cycle --
    cycle_error = engine.cycle_error
    cycle_error_at = _aware(engine.cycle_error_at)
    if cycle_error and cycle_error_at and (enabled or now - cycle_error_at <= timedelta(hours=1)):
        failing = _aware(engine.cycle_failing_since) or cycle_error_at
        errors.append(
            f"De cyclus mislukt sinds {_hm(failing)} UTC: {cycle_error}."
            if failing < cycle_error_at
            else f"De laatste cyclus mislukte om {_hm(cycle_error_at)} UTC: {cycle_error}."
        )
    last_cycle = _aware(engine.last_run) or _aware(scheduled_last)
    planned_from = _aware(scheduled_last) or _aware(engine.last_run)
    # The scheduler starts a due cycle within one tick: a moment in the past means "now".
    next_cycle = max(planned_from + interval, now) if enabled and planned_from else None
    # The planner looks stalled when it started nothing for two intervals (a manual cycle does
    # not count: it says nothing about the scheduler).
    stale = bool(
        enabled
        and not warming_up
        and planned_from is not None
        and now - planned_from > 2 * interval
        and not cycle_error
    )
    if stale:
        from app.web import ago

        warnings.append(
            f"De laatste geplande cyclus was {ago(planned_from, now)}, terwijl die elke "
            f"{settings.trade_interval_min} min hoort te draaien; de planner draait mogelijk niet."
        )

    # -- can it buy? --
    claude_available = bool(engine.decision_maker.available)
    budget_out = bool(budget and budget.get("exhausted"))
    hold = engine.claude_unavailable_mode == "hold"
    if not claude_available or budget_out:
        why = "er is geen Claude-sleutel" if not claude_available else "het Claude-budget is op"
        if hold:
            blockers.append(f"{why}, en zonder Claude wacht de bot bij een koopsignaal af")
        else:
            notes.append(
                f"{why[0].upper() + why[1:]}: bij een koopsignaal volgt de bot alleen de regels, "
                "met de halve inzet."
            )
    resets = rn.get("resets_at") or next_utc_midnight(now)
    blocked_until = None
    if rn.get("orders_limit_reached"):
        blockers.append(orders_limit_nl(rn["orders_today"], rn["max_orders_per_day"]))
        if rn["max_orders_per_day"] > 0:
            blocked_until = resets
    if rn.get("loss_limit_reached"):
        blockers.append(
            f"de dagverlieslimiet van {_num(rn['daily_loss_limit_pct'])}% is bereikt; kopen "
            "kan weer vanaf 00:00 UTC"
        )
        blocked_until = resets
    if rn.get("exposure_limit_reached"):
        in_crypto = _num(rn["exposure_pct"] or 0, 1)
        blockers.append(
            f"de limiet voor crypto in de portefeuille is bereikt ({in_crypto}% van max. "
            f"{_num(rn['max_exposure_pct'])}%)"
        )
    if strategy_invalid is None:
        strategy_invalid = engine.strategy_invalid
    if strategy_invalid:
        blockers.append("de opgeslagen strategie is ongeldig")
    if engine.risk_off_enabled:
        event = active_event(
            engine.calendar, now, engine.risk_off_before_min, engine.risk_off_after_min
        )
        if event is not None:
            until = event.window(0, engine.risk_off_after_min)[1]
            blockers.append(
                f"risk-off rond {event.label}; geen nieuwe aankopen tot {_hm(until)} UTC"
            )

    # -- one status --
    if errors:
        level, label, detail = "error", "Storing", errors[0]
    elif paused:
        level, label, detail = "paused", "Noodstop actief", PAUSED_DETAIL
    elif not enabled:
        level, label, detail = "off", "Uitgeschakeld", OFF_DETAIL
    elif blockers:
        level, label = "warn", "Actief · koopt niet"
        detail = f"Kopen kan nu niet: {blockers[0]}."
    elif warnings:
        level, label, detail = "warn", "Actief", warnings[0]
    else:
        level, label, detail = "ok", "Actief", None

    reasons = list(errors)
    if paused and level != "paused":
        reasons.append("Noodstop actief: kopen gepauzeerd.")
    if not enabled and level != "off":
        reasons.append("Automatisch handelen staat uit.")
    reasons += [f"Kopen kan nu niet: {b}." for b in blockers]
    reasons += warnings + notes

    return {
        "level": level,
        "label": label,
        "detail": detail,
        "reasons": reasons,
        "last_cycle": last_cycle,
        "next_cycle": next_cycle,
        "exits_ok": exits_ok,
        "last_exit_ok": last_exit_ok,
        "exit_interval_min": exit_interval_min,
        # Extras for the pages.
        "paused": paused,
        "enabled": enabled,
        "interval_min": settings.trade_interval_min if enabled else None,
        "buys_blocked": bool(paused or blockers),
        "blockers": blockers,
        "blocked_until": blocked_until,
        "stale": stale,
        "cycle_error": cycle_error,
        # The last failed Claude decision (budget, time-out, no key), None once Claude answers.
        "claude_error": engine.last_error,
        "exit_error": engine.exit_error,
        "exit_failing_since": failing_since,
        "unprotected_count": len(unprotected),
        "claude_available": claude_available,
        "checked_at": now,
    }
