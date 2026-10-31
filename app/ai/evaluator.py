"""Periodic self-evaluation: Claude reviews the journal and proposes strategy changes."""

from __future__ import annotations

import json
from typing import Any

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.budget import BudgetGuard, record_usage
from app.strategy.model import Strategy

SYSTEM_PROMPT = """\
Je bent de evaluator van Crypie, een long-only cryptobot op Bitvavo. Je krijgt de actieve
strategie, de gesloten posities en de beslissingen van de afgelopen periode (met de koers 1, 24
en 72 uur na elke beslissing) en het nieuwssentiment dat daarbij gold.

Je krijgt ook trading_plan (het doel, de eigen regels en de lessen van de eigenaar) en
owner_notes (wat de eigenaar in het trade-journaal bij afzonderlijke trades schreef). Toets de
resultaten aan het plan: zijn de eigen regels gevolgd, en passen de resultaten bij het doel?
Neem de notities serieus als ervaring van de eigenaar en noem het als een les uit het journaal
in de data bevestigd of weersproken wordt. Zijn beide leeg, sla dit over.

Schrijf een eerlijke evaluatie in het Nederlands (report_nl, markdown, maximaal ~500 woorden):
1. Resultaat: rendement, winstpercentage, grootste winst en verlies, vergeleken met kopen-en-houden.
2. Wat werkte en wat niet: welke signalen, welke nieuwssituaties, welke beslissingen van de bot
   zelf (veto's, afgezwakte orders, news exits) goed of slecht uitpakten.
3. Concrete verbeterpunten.

Doe daarnaast, alleen als de data het rechtvaardigt, één voorstel voor een aangepaste strategie
(proposed_strategy) die aan het strategie-schema voldoet: dezelfde structuur als de actieve
strategie, met gewijzigde periodes, drempels, stop-loss of take-profit. Leg in
proposal_rationale_nl uit waarom. Verander niet alles tegelijk; kleine stappen zijn beter te
beoordelen. Bij te weinig data (minder dan circa 10 gesloten posities): geen voorstel, zeg dat
in het rapport.
"""


class EvaluationResult(BaseModel):
    report_nl: str
    proposed_strategy: Strategy | None = None
    proposal_rationale_nl: str = Field(default="")


class Evaluator:
    PURPOSE = "evaluation"

    def __init__(self, api_key: str, model: str, budget: BudgetGuard):
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.model = model
        self.budget = budget

    @property
    def available(self) -> bool:
        return self._client is not None

    async def evaluate(self, session: Session, context: dict[str, Any]) -> EvaluationResult:
        if self._client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        self.budget.ensure_available(session)
        async with self._client.messages.stream(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "Gegevens:\n"
                    + json.dumps(context, ensure_ascii=False, indent=1, default=str),
                }
            ],
            output_format=EvaluationResult,
        ) as stream:
            response = await stream.get_final_message()
        record_usage(session, self.PURPOSE, self.model, response.usage)
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude weigerde de evaluatie")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            text = next((b.text for b in response.content if b.type == "text"), "")
            parsed = EvaluationResult.model_validate_json(text)
        return parsed
