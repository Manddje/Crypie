"""Let Claude turn a plain-language description into a validated Strategy."""

from __future__ import annotations

import json

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.budget import BudgetGuard, record_usage
from app.strategy.model import Strategy

SYSTEM_PROMPT = """\
Je bent de strategie-assistent van Crypie, een trading-app voor Bitvavo.
De gebruiker beschrijft in gewone taal (meestal Nederlands) hoe hij wil handelen.
Jij vertaalt dat naar één strategie-configuratie die precies aan het schema voldoet.

Regels:
- Gebruik alleen de beschikbare indicatortypes: sma, ema, rsi, macd, bollinger.
- Kies ids in kleine letters, bijvoorbeeld "rsi14", "ema50", "macd_std", "bb20".
- macd levert de series "<id>", "<id>_signal" en "<id>_hist"; bollinger levert
  "<id>_upper", "<id>_middle" en "<id>_lower". Prijsseries: open, high, low, close, volume.
- Alle koop-condities moeten tegelijk waar zijn, idem voor verkoop-condities.
- Verwijs in condities alleen naar series die je ook definieert.
- Houd de markt op een van de toegestane markten; interval standaard "1h" tenzij gevraagd.
- Wees conservatief: voeg een stop_loss_pct toe als de gebruiker er niets over zegt.
- Schrijf de toelichting in het Nederlands, kort en concreet: wat doet de strategie,
  waarom deze instellingen, en welke risico's of aannames er zijn.
"""


class StrategyProposal(BaseModel):
    strategy: Strategy
    explanation: str = Field(description="Korte toelichting in het Nederlands")


class StrategyAssistant:
    PURPOSE = "strategy"

    def __init__(self, api_key: str, model: str, budget: BudgetGuard):
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.model = model
        self.budget = budget

    @property
    def available(self) -> bool:
        return self._client is not None

    async def propose(
        self, session: Session, description: str, current: Strategy, markets: list[str]
    ) -> StrategyProposal:
        if self._client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        self.budget.ensure_available(session)
        user = (
            f"Toegestane markten: {', '.join(markets)}.\n\n"
            f"Huidige strategie (ter referentie, mag je aanpassen of vervangen):\n"
            f"{json.dumps(current.model_dump(), ensure_ascii=False, indent=2)}\n\n"
            f"Wens van de gebruiker:\n{description.strip()}"
        )
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
            output_format=StrategyProposal,
        )
        record_usage(session, self.PURPOSE, self.model, response.usage)
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude weigerde dit verzoek te verwerken")
        proposal = response.parsed_output
        if proposal is None:
            raise RuntimeError("Claude gaf geen geldige strategie terug")
        if proposal.strategy.market not in markets:
            raise ValueError(
                f"Claude koos markt {proposal.strategy.market}, "
                f"maar alleen {', '.join(markets)} zijn toegestaan"
            )
        return proposal
