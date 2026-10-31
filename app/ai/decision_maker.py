"""Claude weighs the strategy signal, indicators, news and recent results into a decision."""

from __future__ import annotations

import json
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.budget import BudgetGuard, record_usage

SYSTEM_PROMPT = """\
Je bent de handelsbeslisser van Crypie, een long-only cryptobot op Bitvavo (EUR-markten).
Elke cyclus krijg je voor één markt: de actieve strategie, de huidige indicatorwaarden en het
strategiesignaal, het nieuwssentiment met de belangrijkste berichten, de openstaande positie,
de harde risicolimieten en een samenvatting van je eigen recente beslissingen en hun uitkomst.

Spelregels:
- Kopen mag alleen als het strategiesignaal "buy" is. Jij beslist of je het signaal volgt,
  afzwakt (kleiner bedrag) of veto't op basis van het nieuws en je recente resultaten.
- Verkopen mag bij strategiesignaal "sell", of zonder signaal als het nieuws duidelijk negatief
  en van hoge impact is voor deze coin (news exit). Stop-loss en take-profit worden mechanisch
  afgehandeld, daar hoef jij niets mee.
- Zonder open positie is "sell" zinloos; zonder buy-signaal is "buy" niet toegestaan.
- amount_quote is in EUR en mag nooit boven max_trade_eur liggen; de code kapt het verder af.
- Leer van de samenvatting van recente beslissingen: als vergelijkbare situaties recent slecht
  uitpakten, wees terughoudender; als ze goed uitpakten, mag je het signaal volgen.
- Bij twijfel: "hold". Niet handelen kost niets, een slechte trade wel.
- reasoning_nl: twee tot vier zinnen Nederlands met de doorslaggevende argumenten.
"""


class TradeDecision(BaseModel):
    action: Literal["buy", "sell", "hold"]
    confidence: float = Field(ge=0, le=1)
    amount_quote: float = Field(ge=0, description="EUR to spend (buy) or ignored for sell/hold")
    reasoning_nl: str


class DecisionMaker:
    PURPOSE = "decision"

    def __init__(self, api_key: str, model: str, budget: BudgetGuard):
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.model = model
        self.budget = budget

    @property
    def available(self) -> bool:
        return self._client is not None

    async def decide(self, session: Session, context: dict[str, Any]) -> TradeDecision:
        if self._client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        self.budget.ensure_available(session)
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[
                {
                    "role": "user",
                    "content": "Situatie:\n"
                    + json.dumps(context, ensure_ascii=False, indent=1, default=str),
                }
            ],
            output_format=TradeDecision,
        )
        record_usage(session, self.PURPOSE, self.model, response.usage)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError("Claude gaf geen geldige beslissing terug")
        return response.parsed_output
