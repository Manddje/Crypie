"""Persist the active strategy in the settings table, plus the one it replaced."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import Setting
from app.strategy.model import DEFAULT_STRATEGY, DESCRIPTION_MAX, Strategy

log = logging.getLogger(__name__)

STRATEGY_KEY = "strategy"
# The strategy that was active before the last save, so the owner can put it back.
PREVIOUS_KEY = "strategy:previous"


def _parse(raw: str) -> Strategy:
    """A stored strategy; raises ValueError or ValidationError when it is unusable."""
    data = json.loads(raw)
    if isinstance(data, dict):
        description = data.get("description")
        if isinstance(description, str) and len(description) > DESCRIPTION_MAX:
            # Saved before the length limit existed: shorten rather than drop it.
            data["description"] = description[:DESCRIPTION_MAX]
    return Strategy.model_validate(data)


def load_strategy_checked(session: Session) -> tuple[Strategy, str | None]:
    """The active strategy, plus a Dutch warning when the stored one could not be used.

    Never raises on bad stored data: a strategy that no longer validates (hand-edited JSON,
    an older version, a NaN threshold stored as null) would otherwise break every trading
    cycle, the stop-loss checks included. The default strategy is used instead for exits (the
    trading cycle opens no new positions meanwhile) and the strategy page says so until a
    valid strategy is saved or the default is restored.
    """
    row = session.get(Setting, STRATEGY_KEY)
    if row is None:
        return DEFAULT_STRATEGY, None
    try:
        return _parse(row.value), None
    except (ValueError, ValidationError) as exc:
        log.error("stored strategy is invalid, using the default strategy instead: %s", exc)
        return DEFAULT_STRATEGY, (
            "De opgeslagen strategie is ongeldig en wordt niet gebruikt. De bot opent geen "
            "nieuwe posities; stop-loss, trailing stop en take-profit van open posities volgen "
            f"zolang de standaardstrategie '{DEFAULT_STRATEGY.name}'. Sla een geldige strategie "
            f"op of herstel de standaardstrategie. Fout: {str(exc)[:300]}"
        )


def load_strategy(session: Session) -> Strategy:
    return load_strategy_checked(session)[0]


def strategy_version(session: Session) -> str:
    """An opaque token that changes with every change of the stored strategy. Forms send it
    back, so a save from an old tab cannot silently undo a later change."""
    row = session.get(Setting, STRATEGY_KEY)
    raw = row.value if row is not None else "default"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_previous(session: Session) -> tuple[Strategy, datetime | None] | None:
    """The strategy the last save replaced and when, or None (none kept, or unusable)."""
    row = session.get(Setting, PREVIOUS_KEY)
    if row is None:
        return None
    try:
        data = json.loads(row.value)
        strategy = _parse(json.dumps(data["strategy"]))
        replaced = data.get("replaced_at")
        when = datetime.fromisoformat(replaced) if isinstance(replaced, str) else None
    except (ValueError, ValidationError, KeyError, TypeError):
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return strategy, when


def save_strategy(session: Session, strategy: Strategy, *, keep_previous: bool = True) -> None:
    """Make ``strategy`` the active one and commit (with whatever else the session holds).

    The strategy it replaces is kept as the previous one, unless it was unusable (an invalid
    stored strategy is never offered back) or the same.
    """
    row = session.get(Setting, STRATEGY_KEY)
    payload = strategy.model_dump_json()
    if keep_previous:
        try:
            old = _parse(row.value) if row is not None else DEFAULT_STRATEGY
        except (ValueError, ValidationError):
            old = None
        if old is not None and old.model_dump() != strategy.model_dump():
            kept = json.dumps(
                {
                    "strategy": old.model_dump(mode="json"),
                    "replaced_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
            )
            prev = session.get(Setting, PREVIOUS_KEY)
            if prev is None:
                session.add(Setting(key=PREVIOUS_KEY, value=kept))
            else:
                prev.value = kept
    if row is None:
        session.add(Setting(key=STRATEGY_KEY, value=payload))
    else:
        row.value = payload
    session.commit()
