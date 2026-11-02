"""Persist the active strategy in the settings table."""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import Setting
from app.strategy.model import DEFAULT_STRATEGY, DESCRIPTION_MAX, Strategy

log = logging.getLogger(__name__)

STRATEGY_KEY = "strategy"


def load_strategy_checked(session: Session) -> tuple[Strategy, str | None]:
    """The active strategy, plus a Dutch warning when the stored one could not be used.

    Never raises on bad stored data: a strategy that no longer validates (hand-edited JSON,
    an older version, a NaN threshold stored as null) would otherwise break every trading
    cycle, the stop-loss checks included. The default strategy is used instead and the
    strategy page says so until a valid strategy is saved or the default is restored.
    """
    row = session.get(Setting, STRATEGY_KEY)
    if row is None:
        return DEFAULT_STRATEGY, None
    try:
        data = json.loads(row.value)
        if isinstance(data, dict):
            description = data.get("description")
            if isinstance(description, str) and len(description) > DESCRIPTION_MAX:
                # Saved before the length limit existed: shorten rather than drop it.
                data["description"] = description[:DESCRIPTION_MAX]
        return Strategy.model_validate(data), None
    except (ValueError, ValidationError) as exc:
        log.error("stored strategy is invalid, using the default strategy instead: %s", exc)
        return DEFAULT_STRATEGY, (
            "De opgeslagen strategie is ongeldig en wordt niet gebruikt; de bot handelt nu met "
            f"de standaardstrategie '{DEFAULT_STRATEGY.name}'. Sla een geldige strategie op of "
            f"herstel de standaardstrategie. Fout: {str(exc)[:300]}"
        )


def load_strategy(session: Session) -> Strategy:
    return load_strategy_checked(session)[0]


def save_strategy(session: Session, strategy: Strategy) -> None:
    row = session.get(Setting, STRATEGY_KEY)
    payload = strategy.model_dump_json()
    if row is None:
        session.add(Setting(key=STRATEGY_KEY, value=payload))
    else:
        row.value = payload
    session.commit()
