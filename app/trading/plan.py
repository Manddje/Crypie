"""The trading plan: the owner's own rules, next to the rules the code and strategy enforce.

Everything the bot does mechanically (strategy, limits, dip watch, news triggers) is already
configured elsewhere; the page /plan reads it from there. What is stored here is only the
part no setting can hold: the goal, the owner's own rules of conduct and the lessons learned.
Claude receives those with every decision and every evaluation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import Setting

PLAN_KEY = "trading_plan"

# Field name -> (label, help text, placeholder) for the page and the API.
PLAN_FIELDS: dict[str, tuple[str, str, str]] = {
    "goal": (
        "Doel en horizon",
        "Waarvoor handel je en op welke termijn? Claude gebruikt dit om af te wegen hoeveel "
        "risico past.",
        "Bijvoorbeeld: gestaag vermogen opbouwen in BTC en SOL met kleine dips; kapitaalbehoud "
        "gaat boven rendement. Horizon: maanden, niet uren.",
    ),
    "rules": (
        "Eigen regels",
        "Regels die Claude bij elke beslissing moet volgen, bovenop de strategie. Ze kunnen "
        "Claude terughoudender maken, nooit de harde limieten omzeilen.",
        "Bijvoorbeeld: niet kopen in het weekend; na twee verliezen op rij in dezelfde markt "
        "een dag niets; alleen een volle order bij positief nieuws én opwaartse lange trend.",
    ),
    "avoid": (
        "Wanneer de bot nooit mag handelen",
        "Situaties waarin je nooit wilt kopen of verkopen. Claude kiest dan 'afwachten'.",
        "Bijvoorbeeld: geen aankoop binnen 24 uur na een hack of rechtszaak rond de coin; "
        "nooit bijkopen in een positie die onder water staat.",
    ),
    "lessons": (
        "Lessen",
        "Wat je uit het journaal hebt geleerd. Houd het kort en concreet; Claude leest dit "
        "mee als ervaring van de eigenaar.",
        "Bijvoorbeeld: dips op FARTCOIN lopen vaak door, daar kleiner instappen; take-profit "
        "van 8% werd op SOL drie keer net gemist.",
    ),
}


# Maximum length per field; the form and the API share these.
PLAN_MAX_LENGTH: dict[str, int] = {"goal": 2000, "rules": 4000, "avoid": 4000, "lessons": 4000}


class TradingPlan(BaseModel):
    goal: str = Field(default="", max_length=PLAN_MAX_LENGTH["goal"])
    rules: str = Field(default="", max_length=PLAN_MAX_LENGTH["rules"])
    avoid: str = Field(default="", max_length=PLAN_MAX_LENGTH["avoid"])
    lessons: str = Field(default="", max_length=PLAN_MAX_LENGTH["lessons"])
    updated_at: datetime | None = None

    def is_empty(self) -> bool:
        return not any(getattr(self, k).strip() for k in PLAN_FIELDS)

    def as_context(self) -> dict[str, Any] | None:
        """What Claude gets: only the fields with text, or None when nothing is written yet."""
        filled = {k: getattr(self, k).strip() for k in PLAN_FIELDS if getattr(self, k).strip()}
        if not filled:
            return None
        return {
            "note": "eigen regels van de eigenaar; ze kunnen je terughoudender maken, nooit "
            "iets toestaan wat de spelregels of de harde limieten verbieden",
            **filled,
        }


def load_plan(session: Session) -> TradingPlan:
    row = session.get(Setting, PLAN_KEY)
    if row is None:
        return TradingPlan()
    plan = TradingPlan.model_validate_json(row.value)
    plan.updated_at = row.updated_at
    return plan


def save_plan(session: Session, plan: TradingPlan) -> TradingPlan:
    payload = plan.model_dump_json(exclude={"updated_at"})
    row = session.get(Setting, PLAN_KEY)
    if row is None:
        row = Setting(key=PLAN_KEY, value=payload)
        session.add(row)
    else:
        row.value = payload
    session.commit()
    plan.updated_at = row.updated_at
    return plan
