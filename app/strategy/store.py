"""Persist the active strategy in the settings table."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import Setting
from app.strategy.model import DEFAULT_STRATEGY, Strategy

STRATEGY_KEY = "strategy"


def load_strategy(session: Session) -> Strategy:
    row = session.get(Setting, STRATEGY_KEY)
    if row is None:
        return DEFAULT_STRATEGY
    return Strategy.model_validate_json(row.value)


def save_strategy(session: Session, strategy: Strategy) -> None:
    row = session.get(Setting, STRATEGY_KEY)
    payload = strategy.model_dump_json()
    if row is None:
        session.add(Setting(key=STRATEGY_KEY, value=payload))
    else:
        row.value = payload
    session.commit()
