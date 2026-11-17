"""The bot's strategies: each one its rules (JSON), its markets, its own budget and a switch.

Stored in the ``strategies`` table (one row per strategy, :class:`app.db.StrategyRecord`).
An installation from before this table had one strategy in the settings table; the first
load moves it over as the first strategy, with no markets of its own (so it keeps trading
every tracked market) and no budget of its own (so only the general limits apply).

Which strategy trades a market: the enabled strategy that names the market (a market can be
named by one enabled strategy at a time); a market no strategy names goes to the enabled
strategy without markets of its own (the "rest" strategy, at most one), if any. A market
without a strategy is not traded, but open positions in it are still guarded by the exits.

The functions at the bottom (``load_strategy``, ``save_strategy``, ...) keep the old
one-strategy interface: they work on the first strategy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Setting, StrategyRecord
from app.strategy.model import DEFAULT_STRATEGY, DESCRIPTION_MAX, Strategy

log = logging.getLogger(__name__)

# The settings keys of the one strategy of an older installation (read once, on migration).
STRATEGY_KEY = "strategy"
PREVIOUS_KEY = "strategy:previous"

MAX_STRATEGIES = 10
MARKET_RE = re.compile(r"^[A-Z0-9]{2,10}-EUR$")
# Bitvavo's minimum order; a budget below it can never buy anything.
MIN_BUDGET_EUR = 5.0
MAX_BUDGET_EUR = 10_000_000.0


@dataclass
class StrategySlot:
    """One strategy as the app uses it: the parsed rules plus its money and markets.

    ``warning`` is set when the stored JSON could not be used (hand-edited, an older version):
    ``strategy`` is then the default strategy, which only serves the exits of open positions;
    the trading cycle opens no new positions for this strategy until a valid one is saved.
    """

    id: int
    strategy: Strategy
    budget_eur: float = 0.0
    markets: list[str] = field(default_factory=list)  # explicit; [] = every unclaimed market
    enabled: bool = True
    warning: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    previous: Strategy | None = None
    previous_at: datetime | None = None
    # Filled by assign_markets(): the tracked markets this strategy trades right now.
    active_markets: list[str] | None = None

    @property
    def name(self) -> str:
        return self.strategy.name

    @property
    def catch_all(self) -> bool:
        """Trades every tracked market that no other enabled strategy names."""
        return not self.markets

    @property
    def usable(self) -> bool:
        """Switched on and with rules that validate: may open positions."""
        return self.enabled and self.warning is None

    def scope_nl(self) -> str:
        return "alle overige markten" if self.catch_all else ", ".join(self.markets)


# -- parsing ---------------------------------------------------------------------------------


def _parse(raw: str) -> Strategy:
    """A stored strategy; raises ValueError or ValidationError when it is unusable."""
    data = json.loads(raw)
    if isinstance(data, dict):
        description = data.get("description")
        if isinstance(description, str) and len(description) > DESCRIPTION_MAX:
            # Saved before the length limit existed: shorten rather than drop it.
            data["description"] = description[:DESCRIPTION_MAX]
    return Strategy.model_validate(data)


def _invalid_warning(exc: Exception) -> str:
    return (
        "De opgeslagen strategie is ongeldig en wordt niet gebruikt. De bot opent geen "
        "nieuwe posities; stop-loss, trailing stop en take-profit van open posities volgen "
        f"zolang de standaardstrategie '{DEFAULT_STRATEGY.name}'. Sla een geldige strategie "
        f"op of herstel de standaardstrategie. Fout: {str(exc)[:300]}"
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _slot_from_row(row: StrategyRecord) -> StrategySlot:
    warning = None
    try:
        strategy = _parse(row.config)
    except (ValueError, ValidationError) as exc:
        log.error(
            "stored strategy is invalid (id %s), using the default strategy instead: %s",
            row.id,
            exc,
        )
        strategy, warning = DEFAULT_STRATEGY, _invalid_warning(exc)
    previous = None
    if row.previous:
        try:
            previous = _parse(row.previous)
        except (ValueError, ValidationError):
            previous = None
    return StrategySlot(
        id=row.id,
        strategy=strategy,
        budget_eur=float(row.budget_eur or 0.0),
        markets=row.market_list(),
        enabled=bool(row.enabled),
        warning=warning,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        previous=previous,
        previous_at=_aware(row.previous_at) if previous is not None else None,
    )


# -- loading ---------------------------------------------------------------------------------


def _legacy_previous(session: Session) -> tuple[str | None, datetime | None]:
    row = session.get(Setting, PREVIOUS_KEY)
    if row is None:
        return None, None
    try:
        data = json.loads(row.value)
        raw = json.dumps(data["strategy"])
        _parse(raw)
        replaced = data.get("replaced_at")
        when = datetime.fromisoformat(replaced) if isinstance(replaced, str) else None
    except (ValueError, ValidationError, KeyError, TypeError):
        return None, None
    return raw, _aware(when)


def ensure_strategies(session: Session) -> None:
    """Make sure there is at least one strategy: the one an older installation kept in the
    settings table (its stored JSON as it is, even when it no longer validates, so the owner
    sees the warning and can fix it), else the default strategy."""
    if session.scalar(select(StrategyRecord.id).limit(1)) is not None:
        return
    legacy = session.get(Setting, STRATEGY_KEY)
    config = legacy.value if legacy is not None else DEFAULT_STRATEGY.model_dump_json()
    previous, previous_at = _legacy_previous(session)
    session.add(
        StrategyRecord(
            config=config,
            budget_eur=0.0,
            markets="",
            enabled=True,
            previous=previous,
            previous_at=previous_at,
        )
    )
    session.commit()
    if legacy is not None:
        log.info("moved the strategy from the settings table to the strategies table")


def load_slots(session: Session) -> list[StrategySlot]:
    """Every strategy, oldest first (the first one is the "primary" strategy)."""
    ensure_strategies(session)
    rows = session.scalars(select(StrategyRecord).order_by(StrategyRecord.id)).all()
    return [_slot_from_row(r) for r in rows]


def load_slot(session: Session, slot_id: int) -> StrategySlot | None:
    row = session.get(StrategyRecord, slot_id)
    return _slot_from_row(row) if row is not None else None


def primary_slot(session: Session) -> StrategySlot:
    return load_slots(session)[0]


def assign_markets(slots: list[StrategySlot], all_markets: list[str]) -> dict[str, StrategySlot]:
    """market -> the enabled strategy that trades it, for the tracked ``all_markets``.

    A market named by an enabled strategy goes to that strategy (the oldest one, should two
    name it: saving refuses that, but stored data is never trusted blindly). The markets left
    over go to the oldest enabled strategy without markets of its own.
    """
    plan: dict[str, StrategySlot] = {}
    tracked = list(dict.fromkeys(all_markets))
    for slot in slots:
        if not slot.enabled or slot.catch_all:
            continue
        for market in slot.markets:
            if market in tracked and market not in plan:
                plan[market] = slot
    rest = next((s for s in slots if s.enabled and s.catch_all), None)
    if rest is not None:
        for market in tracked:
            plan.setdefault(market, rest)
    for slot in slots:
        slot.active_markets = [m for m, s in plan.items() if s is slot]
    return plan


def market_map(session: Session, all_markets: list[str]) -> dict[str, StrategySlot]:
    return assign_markets(load_slots(session), all_markets)


def strategy_for_market(
    session: Session, market: str, all_markets: list[str]
) -> StrategySlot | None:
    return market_map(session, all_markets).get(market)


def resolved_markets(
    slot: StrategySlot, slots: list[StrategySlot], all_markets: list[str]
) -> list[str]:
    """The tracked markets this strategy trades now (enabled), or would trade once switched
    on: its own markets that are tracked, or for a "rest" strategy whatever the others leave."""
    tracked = list(dict.fromkeys(all_markets))
    if not slot.catch_all:
        return [m for m in slot.markets if m in tracked]
    claimed = {
        m for s in slots if s.enabled and not s.catch_all and s.id != slot.id for m in s.markets
    }
    return [m for m in tracked if m not in claimed]


def exit_slot_for(
    position_market: str,
    position_strategy_id: int | None,
    plan: dict[str, StrategySlot],
    slots: list[StrategySlot],
) -> StrategySlot | None:
    """Whose rules guard an open position: the strategy that trades its market now, else the
    one that opened it (even when switched off), else the first strategy. None without any."""
    slot = plan.get(position_market)
    if slot is not None:
        return slot
    if position_strategy_id is not None:
        slot = next((s for s in slots if s.id == position_strategy_id), None)
        if slot is not None:
            return slot
    return slots[0] if slots else None


# -- validation ------------------------------------------------------------------------------


def parse_markets(raw: object) -> list[str]:
    """A markets value from a form or the API ('BTC-EUR, eth-eur' or a list) as a clean
    list; raises ValueError (Dutch)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list | tuple) else str(raw).replace(";", ",").split(",")
    markets: list[str] = []
    for item in items:
        m = str(item).strip().upper()
        if not m:
            continue
        if not MARKET_RE.match(m):
            raise ValueError(f"{m} is geen euromarkt in Bitvavo-notatie (bijvoorbeeld BTC-EUR)")
        if m not in markets:
            markets.append(m)
    return markets


def parse_budget(raw: object) -> float:
    """A budget from a form or the API; 0 = no budget of its own. Raises ValueError (Dutch)."""
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(str(raw).replace(",", ".")) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("het budget is geen geldig getal") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("het budget moet 0 of een positief bedrag zijn")
    if 0 < value < MIN_BUDGET_EUR:
        raise ValueError(
            f"een budget is minstens € {MIN_BUDGET_EUR:.0f} (of 0 = geen eigen budget)"
        )
    if value > MAX_BUDGET_EUR:
        raise ValueError("het budget is te hoog")
    return round(value, 2)


def market_problems(
    slots: list[StrategySlot],
    markets: list[str],
    *,
    slot_id: int | None,
    enabled: bool,
    all_markets: list[str],
) -> list[str]:
    """Why a strategy may not have these markets (Dutch), or an empty list: a market the bot
    does not track, a market another enabled strategy already names, or a second enabled
    strategy without markets of its own."""
    problems: list[str] = []
    unknown = [m for m in markets if m not in all_markets]
    if unknown:
        problems.append(
            f"{', '.join(unknown)} staat niet bij de markten van de bot "
            f"(MARKETS: {', '.join(all_markets)})"
        )
    if not enabled:
        return problems
    others = [s for s in slots if s.enabled and s.id != slot_id]
    if not markets:
        rest = next((s for s in others if s.catch_all), None)
        if rest is not None:
            problems.append(
                f"strategie '{rest.name}' handelt al op alle overige markten; geef deze "
                "strategie eigen markten of zet die andere uit"
            )
        return problems
    for other in others:
        overlap = [m for m in markets if m in other.markets]
        if overlap:
            problems.append(
                f"{', '.join(overlap)} hoort al bij strategie '{other.name}'; een markt kan "
                "maar door één strategie tegelijk worden gehandeld"
            )
    return problems


# -- saving ----------------------------------------------------------------------------------


def _row(session: Session, slot_id: int) -> StrategyRecord:
    row = session.get(StrategyRecord, slot_id)
    if row is None:
        raise LookupError(f"Strategie {slot_id} bestaat niet.")
    return row


def save_slot(
    session: Session,
    slot_id: int,
    strategy: Strategy | None = None,
    *,
    budget_eur: float | None = None,
    markets: list[str] | None = None,
    enabled: bool | None = None,
    keep_previous: bool = True,
) -> StrategySlot:
    """Change a strategy and commit (with whatever else the session holds). Only the given
    parts change. New rules replace the old ones, which are kept as the previous rules
    (unless they were unusable or the same). Raises LookupError for an unknown id."""
    row = _row(session, slot_id)
    if strategy is not None:
        payload = strategy.model_dump_json()
        if keep_previous:
            try:
                old = _parse(row.config)
            except (ValueError, ValidationError):
                old = None
            if old is not None and old.model_dump() != strategy.model_dump():
                row.previous = old.model_dump_json()
                row.previous_at = datetime.now(UTC)
        row.config = payload
    if budget_eur is not None:
        row.budget_eur = float(budget_eur)
    if markets is not None:
        row.markets = ",".join(markets)
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(row)
    return _slot_from_row(row)


def add_slot(
    session: Session,
    strategy: Strategy,
    *,
    budget_eur: float = 0.0,
    markets: list[str] | None = None,
    enabled: bool = True,
) -> StrategySlot:
    """A new strategy; raises ValueError when there are already MAX_STRATEGIES."""
    ensure_strategies(session)
    count = len(session.scalars(select(StrategyRecord.id)).all())
    if count >= MAX_STRATEGIES:
        raise ValueError(f"Er kunnen hoogstens {MAX_STRATEGIES} strategieën zijn.")
    row = StrategyRecord(
        config=strategy.model_dump_json(),
        budget_eur=float(budget_eur),
        markets=",".join(markets or []),
        enabled=bool(enabled),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _slot_from_row(row)


def delete_slot(session: Session, slot_id: int) -> None:
    """Remove a strategy; the last one cannot go (the bot needs rules for its exits).
    Positions it opened keep their strategy_id; their exits fall back to another strategy."""
    row = _row(session, slot_id)
    if len(session.scalars(select(StrategyRecord.id)).all()) <= 1:
        raise ValueError(
            "De laatste strategie kan niet worden verwijderd; pas hem aan of zet hem uit."
        )
    session.delete(row)
    session.commit()


def slot_version(session: Session, slot_id: int) -> str:
    """An opaque token that changes with every change of a strategy's rules. Forms send it
    back, so a save from an old tab cannot silently undo a later change."""
    row = session.get(StrategyRecord, slot_id)
    raw = f"{slot_id}:{row.config}" if row is not None else f"{slot_id}:missing"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def restore_previous(session: Session, slot_id: int) -> StrategySlot:
    """Swap a strategy's rules with its previous rules. Raises ValueError without any."""
    slot = load_slot(session, slot_id)
    if slot is None:
        raise LookupError(f"Strategie {slot_id} bestaat niet.")
    if slot.previous is None:
        raise ValueError("Er is geen vorige strategie bewaard.")
    return save_slot(session, slot_id, slot.previous)


# -- the one-strategy interface: the first strategy ------------------------------------------


def load_strategy_checked(session: Session) -> tuple[Strategy, str | None]:
    """The first strategy, plus a Dutch warning when its stored rules could not be used.

    Never raises on bad stored data: a strategy that no longer validates would otherwise
    break every trading cycle, the stop-loss checks included. The default strategy stands in
    for the exits (no new positions are opened meanwhile) and the pages say so.
    """
    slot = primary_slot(session)
    return slot.strategy, slot.warning


def load_strategy(session: Session) -> Strategy:
    return primary_slot(session).strategy


def strategy_version(session: Session) -> str:
    return slot_version(session, primary_slot(session).id)


def load_previous(session: Session) -> tuple[Strategy, datetime | None] | None:
    """The rules the first strategy's last save replaced and when, or None."""
    slot = primary_slot(session)
    if slot.previous is None:
        return None
    return slot.previous, slot.previous_at


def save_strategy(session: Session, strategy: Strategy, *, keep_previous: bool = True) -> None:
    """Make ``strategy`` the rules of the first strategy and commit."""
    slot = primary_slot(session)
    save_slot(session, slot.id, strategy, keep_previous=keep_previous)
