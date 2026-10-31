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
- Kopen mag als het strategiesignaal "buy" is. Jij beslist of je het signaal volgt,
  afzwakt (kleiner bedrag) of veto't op basis van het nieuws en je recente resultaten.
- signal_checks toont elke strategievoorwaarde met de echte waarden. Voorwaarden met
  "soft": true zijn filters, geen eisen: staat er iets in soft_filters_failed (bijvoorbeeld
  de koers onder de EMA200, dus een dip in een dalende trend), dan weeg je dat af tegen het
  nieuws. Koop in dat geval alleen bij neutraal tot positief nieuws en met een kleiner bedrag;
  bij negatief nieuws is "hold" de keuze. De code halveert het bedrag dan sowieso.
- Kopen mag ook zonder buy-signaal als news_entry_allowed true is: het nieuws voor deze coin is
  dan sterk positief en van hoge impact. Doe dat alleen als de berichten echt koersrelevant zijn
  en de indicatoren niet tegenwerken; de code beperkt zo'n nieuwsaankoop tot een kleiner bedrag.
  Slecht nieuws (hack, rechtszaak, verbod) is geen reden om "de dip te kopen".
- Verkopen mag bij strategiesignaal "sell", of zonder signaal als het nieuws duidelijk negatief
  en van hoge impact is voor deze coin (news exit). Stop-loss en take-profit worden mechanisch
  afgehandeld, daar hoef jij niets mee.
- Zonder open positie is "sell" zinloos; zonder buy-signaal en zonder news_entry_allowed is
  "buy" niet toegestaan.
- amount_quote is in EUR en mag nooit boven max_trade_eur liggen; de code kapt het verder af.
- Leer van de samenvatting van recente beslissingen: als vergelijkbare situaties recent slecht
  uitpakten, wees terughoudender; als ze goed uitpakten, mag je het signaal volgen.
- Transactiekosten tellen: "costs" geeft de fee per kant en wat een rondje kopen+verkopen
  samen kost. Een trade is pas zinvol als de verwachte beweging daar ruim boven ligt; de
  genoemde winsten en unrealized_net_pct zijn al netto na kosten.
- recent_candles.price_context zegt hoe ver de koers onder het 7- en 30-daags hoog staat en
  hoe hij t.o.v. het 30-daags gemiddelde ligt. Een lagere koers dan vorige week is op zich geen
  koopreden (de markt kan verder dalen); het telt pas mee als er ook een draai zichtbaar is of
  het nieuws positief is. Dan mag een flinke korting je wél iets meer vertrouwen geven.
- Koop niet in een vallend mes. recent_candles toont de laatste candles (oudste eerst):
  dalen koers en oscillatoren (zoals RSI) nog steeds en staat de koers vlak bij het 24-uurs
  dieptepunt, dan is "hold" de keuze en wacht je een cyclus op een draai. Alleen sterk positief
  nieuws van hoge impact rechtvaardigt eerder instappen, en dan met een kleiner bedrag.
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
