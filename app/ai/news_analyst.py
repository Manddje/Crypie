"""Score news items with Claude: relevance, sentiment, impact, horizon and affected coins."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.budget import BudgetGuard, record_usage
from app.db import NewsItem

SYSTEM_PROMPT = """\
Je bent de nieuwsanalist van Crypie, een cryptohandelsbot op Bitvavo (handel in EUR).
Je krijgt een lijst nieuwsberichten (titel, bron, eventueel samenvatting). Beoordeel per
bericht wat het betekent voor de koers van de opgegeven coins in de komende uren tot weken.

Per bericht lever je:
- id: het id uit de invoer, ongewijzigd.
- relevant: false als het bericht de koers van deze coins redelijkerwijs niet raakt
  (entertainment, sport, lokaal nieuws zonder macro-effect, reclame, prijsvoorspelling zonder
  nieuwsfeit). Dan sentiment 0, impact 0, coins leeg.
- sentiment: -1.0 (zeer negatief voor de koers) tot 1.0 (zeer positief), 0 neutraal.
- impact: 0 geen, 1 klein, 2 merkbaar, 3 groot (hacks van grote beurzen, regulering, rentebesluiten,
  ETF-beslissingen, grote liquidaties, geopolitieke schokken).
- horizon: "hours", "days" of "weeks": hoe lang het effect naar verwachting doorwerkt.
- coins: welke van de opgegeven coins het raakt, of ["ALL"] als het de hele markt raakt.
- summary_nl: één zin Nederlands die het nieuwsfeit en het verwachte koerseffect noemt.
- event_key: korte sleutel voor het nieuwsfeit, kleine letters en koppeltekens, twee tot vier
  woorden (bijvoorbeeld "binance-hack", "fed-renteverlaging", "btc-etf-instroom"). Berichten over
  hetzelfde feit krijgen dezelfde sleutel, ook als de kop anders is of de bron een andere invalshoek
  kiest (reacties, analyses en vervolgberichten over dat feit horen erbij). Staat het feit al in de
  lijst met bestaande sleutels, gebruik dan precies die sleutel. Niet relevant: lege sleutel.

Wees nuchter: de meeste berichten zijn ruis (impact 0 of 1). Herhalingen van hetzelfde feit
krijgen dezelfde score en dezelfde event_key; de bot telt elk feit één keer, hoeveel bronnen het
ook overnemen. Baseer je alleen op de tekst, niet op eigen koersvoorspellingen.
"""


def normalize_key(text: str) -> str:
    """Lowercase, letters/digits only, words joined by '-', at most 64 characters."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words)[:64].rstrip("-")


class Assessment(BaseModel):
    id: int
    relevant: bool
    sentiment: float = Field(ge=-1, le=1)
    impact: int = Field(ge=0, le=3)
    horizon: Literal["hours", "days", "weeks"] = "days"
    coins: list[str] = Field(default_factory=list)
    summary_nl: str = ""
    event_key: str = ""


class AssessmentBatch(BaseModel):
    assessments: list[Assessment]


class NewsAnalyst:
    PURPOSE = "news"

    def __init__(self, api_key: str, model: str, budget: BudgetGuard):
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.model = model
        self.budget = budget

    @property
    def available(self) -> bool:
        return self._client is not None

    @staticmethod
    def _render(items: list[NewsItem]) -> str:
        rows = []
        for it in items:
            row = {"id": it.id, "source": it.source_name, "title": it.title}
            if it.summary:
                row["summary"] = it.summary[:600]
            if it.source_coins:
                row["source_tags"] = it.source_coins
            rows.append(row)
        return json.dumps(rows, ensure_ascii=False, indent=1)

    async def assess(
        self,
        session: Session,
        items: list[NewsItem],
        coins: list[str],
        known_keys: list[str] | None = None,
    ) -> list[Assessment]:
        if self._client is None:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        if not items:
            return []
        self.budget.ensure_available(session)
        known = (
            f"Bestaande sleutels (event_key) van de laatste 48 uur: {', '.join(known_keys)}.\n"
            if known_keys
            else ""
        )
        user = (
            f"Coins waarop we handelen: {', '.join(coins)}.\n"
            f"{known}"
            f"Beoordeel elk van deze {len(items)} berichten:\n{self._render(items)}"
        )
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user}],
            output_format=AssessmentBatch,
        )
        record_usage(session, self.PURPOSE, self.model, response.usage)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError("Claude gaf geen geldige beoordeling terug")
        return response.parsed_output.assessments


def apply_assessments(
    session: Session, items: list[NewsItem], assessments: list[Assessment], model: str
) -> int:
    """Write Claude's answers onto the items. Items Claude skipped stay unassessed."""
    by_id = {a.id: a for a in assessments}
    now = datetime.now(UTC)
    applied = 0
    for it in items:
        a = by_id.get(it.id)
        if a is None:
            continue
        it.assessed = True
        it.assessed_at = now
        it.assessment_model = model
        it.relevant = a.relevant
        it.sentiment = a.sentiment if a.relevant else 0.0
        it.impact = a.impact if a.relevant else 0
        it.horizon = a.horizon
        it.coins = ",".join(sorted({c.upper() for c in a.coins})) if a.relevant else ""
        it.summary_nl = a.summary_nl
        it.event_key = normalize_key(a.event_key) or None if a.relevant else None
        applied += 1
    session.commit()
    return applied
