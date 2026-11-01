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
- Gebruik alleen de beschikbare indicatortypes: sma, ema, rsi, macd, bollinger, drawdown,
  fibonacci, ema_support
  (drawdown = procent onder de hoogste koers van de laatste "period" candles; handig als
  "korting" t.o.v. het recente hoog, bij voorkeur als zacht filter, nooit als enige koopreden).
- fibonacci (velden: id, period) meet de Fibonacci-retracement van de laatste swing binnen
  "period" candles en levert de series "<id>" (procent van de swing dat is teruggelopen; 38,2,
  50 en 61,8 zijn de klassieke niveaus, 100 = terug bij het begin van de swing), "<id>_up"
  (1 = opwaartse swing, de koers valt terug naar steun; 0 = neerwaartse swing, de koers veert op
  naar weerstand), "<id>_high", "<id>_low", "<id>_382", "<id>_500", "<id>_618" (niveaukoersen)
  en "<id>_near" (procent afstand tot het dichtstbijzijnde niveau). "Kopen in de golden pocket"
  is: "<id>" >= 38.2 én "<id>" <= 61.8 én "<id>_up" > 0, bij voorkeur als zachte filters naast
  een harde dip- of draaivoorwaarde zoals RSI.
- ema_support (velden: id, period, tolerance_pct) gebruikt een EMA als dynamische steun en
  levert "<id>" (procent dat de koers boven de EMA staat, negatief = eronder), "<id>_ema" en
  "<id>_bounce" (1 als de koers deze candle de EMA binnen tolerance_pct raakte en erboven sloot:
  steun getest en gehouden). "Koers veert op van de EMA50" is "<id>_bounce" > 0; "koers vlak
  boven de EMA50" is "<id>" >= 0 én "<id>" <= 2.
- Kies ids in kleine letters, bijvoorbeeld "rsi14", "ema50", "macd_std", "bb20".
- macd levert de series "<id>", "<id>_signal" en "<id>_hist"; bollinger levert
  "<id>_upper", "<id>_middle" en "<id>_lower". Prijsseries: open, high, low, close, volume.
- Alle koop-condities moeten tegelijk waar zijn, idem voor verkoop-condities. Een conditie
  met "soft": true is een zacht filter: het signaal ontstaat ook zonder, Claude ziet of het
  gold en handelt kleiner als dat niet zo is. Gebruik dat voor trendfilters (koers boven een
  EMA) als de gebruiker ook in een daling wil kunnen kopen; minstens één conditie blijft hard.
- Operators: <, <=, >, >=, crosses_above, crosses_below, rising, falling. Bij rising/falling is
  "right" het aantal candles terug: {"left": "rsi14", "op": "rising", "right": 1} betekent dat
  de RSI hoger is dan één candle eerder (bevestiging dat een dip draait, geen vallend mes).
- Verwijs in condities alleen naar series die je ook definieert.
- Houd de markt op een van de toegestane markten; interval standaard "1h" tenzij gevraagd.
- Wees conservatief: voeg een stop_loss_pct toe als de gebruiker er niets over zegt.
- trailing_stop_pct laat de stop meelopen onder het hoogste punt sinds instap (hij gaat nooit
  omlaag); trailing_start_pct laat dat pas beginnen zodra de winst zo groot is geweest. Gebruik
  dit als de gebruiker winst wil laten doorlopen in plaats van een vaste take-profit, en kies
  voor een verkoopsignaal op RSI liever "RSI boven 70 én falling" dan alleen "RSI boven 70":
  een hoge RSI betekent een sterke stijging, pas de draai omlaag is het signaal.
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
