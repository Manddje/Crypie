"""Claude's window on Crypie: an MCP server (Model Context Protocol) at ``POST /mcp``.

Claude Desktop and Claude Code connect to it to talk about what the bot is doing. Every tool
is a thin wrapper around the JSON API of app/main.py, called in-process with the same bearer
token (app/auth/api_token.py), so the API's validation, limits and step-up (the current
password for sensitive changes) apply unchanged, and the MCP can never do more than the API.

The transport is the stateless, JSON-only form of Streamable HTTP: one POST per JSON-RPC
request, answered with ``application/json`` (never a server-initiated SSE stream, so GET and
DELETE answer 405 and there is no session id). Of the protocol only what a tools-only server
needs is spoken: ``initialize``, ``ping``, ``tools/list`` and ``tools/call``. That is small
enough to keep in-house: the official SDK would bring a dozen hash-pinned packages into the
hardened image for the same four methods.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse, Response

from app.news.service import NEWS_RETENTION_DAYS
from app.strategy.model import DESCRIPTION_MAX, INTERVALS, MAX_CANDLES, Strategy
from app.trading.plan import PLAN_MAX_LENGTH

log = logging.getLogger(__name__)

router = APIRouter()

MCP_PATH = "/mcp"
SERVER_NAME = "crypie"
# The versions this server speaks; a client asking for another gets DEFAULT_PROTOCOL back.
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL = "2025-06-18"
VERSION_HEADER = "mcp-protocol-version"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

NEWS_HOURS_MAX = NEWS_RETENTION_DAYS * 24
MARKET_PATTERN = r"^[A-Za-z0-9]{2,10}-[Ee][Uu][Rr]$"
CandleInterval = Literal[(*INTERVALS, "1W", "1M")]  # type: ignore[valid-type]
ChartPeriod = Literal["1d", "7d", "30d", "90d"]

INSTRUCTIONS = """\
Crypie is een handelsbot voor Bitvavo (crypto, in euro's) die indicatoren, nieuws en Claude
combineert tot koop- en verkoopbeslissingen. De bot draait in oefenmodus (paper: gesimuleerde
orders) of live (echte orders). Alles wat de bot doet staat in dit MCP: zijn status en limieten,
open en gesloten posities, de beslissingen met Claude's redenering, orders, het trade-journaal,
nieuws en sentiment, de strategieën (elk met eigen markten, budget en regels), het tradingplan
van de eigenaar, de periodieke zelfevaluaties en de Claude-kosten.

Gebruik eerst de leestools (status, positions, decisions, journal, news_sentiment) om te zien wat
er speelt, en spar daarmee met de eigenaar. Tools die iets doen (een order, de strategie,
instellingen, de noodstop) voer je alleen uit als de eigenaar daar in dit gesprek om vraagt;
noem vooraf wat er gaat gebeuren. Gevoelige wijzigingen (modus, markten, harde limieten,
handelskosten, bescherming, Claude-budget, RSS-feeds, handmatige orders in live-modus) vragen
het huidige wachtwoord van de eigenaar als `current_password`; vraag dat nooit uit eigen
beweging. Bedragen zijn euro's, percentages zijn procenten, tijden zijn UTC, teksten in de
antwoorden zijn Nederlands."""


# -- tool inputs -----------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketIn(_In):
    market: str = Field(
        pattern=MARKET_PATTERN, description="Markt in Bitvavo-notatie, bijv. BTC-EUR"
    )


class CandlesIn(MarketIn):
    interval: CandleInterval = Field(default="1h", description="Candle-interval")
    limit: int = Field(
        default=100, ge=1, le=MAX_CANDLES, description="Aantal candles (de nieuwste)"
    )


class ChartIn(MarketIn):
    period: ChartPeriod = Field(default="7d", description="Periode van de grafiek")


class PeriodIn(_In):
    period: ChartPeriod = Field(default="7d", description="Periode")


class ModeIn(_In):
    mode: Literal["paper", "live"] | None = Field(
        default=None, description="paper of live; leeg = de modus waarin de bot nu handelt"
    )


CURSOR_FIELD = Field(
    default=None,
    max_length=200,
    description="Volgende pagina: de cursor die het vorige antwoord noemde",
)


class PositionsIn(ModeIn):
    days: int = Field(default=30, ge=1, le=3650, description="Gesloten posities van N dagen")
    limit: int = Field(default=200, ge=1, le=1000, description="Gesloten posities per pagina")
    cursor: str | None = CURSOR_FIELD


class OrdersIn(_In):
    limit: int = Field(default=50, ge=1, le=500, description="Aantal orders (de nieuwste)")
    mode: Literal["paper", "live"] | None = Field(
        default=None, description="Alleen paper of live; leeg = allebei"
    )
    cursor: str | None = CURSOR_FIELD


class OrderIdIn(_In):
    order_id: int = Field(ge=1, description="id uit orders of uit place_order")


class EvaluationsIn(_In):
    limit: int = Field(default=20, ge=1, le=100, description="Aantal evaluaties (de nieuwste)")
    cursor: str | None = CURSOR_FIELD


class DecisionsIn(_In):
    limit: int = Field(default=50, ge=1, le=500, description="Aantal beslissingen (de nieuwste)")
    cursor: str | None = CURSOR_FIELD


class JournalIn(_In):
    market: str | None = Field(default=None, max_length=16, description="Alleen deze markt")
    status: Literal["open", "closed"] | None = Field(default=None, description="open of closed")
    result: Literal["win", "loss"] | None = Field(
        default=None, description="win of loss (netto, na kosten)"
    )
    reason: Literal["stop_loss", "trailing_stop", "take_profit", "signal", "manual"] | None = Field(
        default=None, description="Hoe de positie gesloten is"
    )
    by: Literal["bot", "manual"] | None = Field(default=None, description="Wie de positie opende")
    days: int | None = Field(default=None, ge=0, le=3650, description="Alleen de laatste N dagen")
    tag: str | None = Field(
        default=None, max_length=64, description="Alleen posities met dit label"
    )
    q: str | None = Field(
        default=None, max_length=200, description="Zoekterm in notities en redenen"
    )
    mode: Literal["paper", "live"] | None = Field(default=None, description="paper of live")
    limit: int = Field(default=200, ge=1, le=1000)


class NewsIn(_In):
    hours: int = Field(
        default=24, ge=1, le=NEWS_HOURS_MAX, description="Berichten van de laatste N uur"
    )
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = CURSOR_FIELD


class SentimentIn(_In):
    hours: int = Field(default=48, ge=1, le=NEWS_HOURS_MAX, description="Venster in uren")


class PauseIn(_In):
    paused: bool = Field(
        description="true = noodstop (geen nieuwe aankopen), false = kopen hervatten"
    )


class EvaluationIn(_In):
    days: int = Field(default=7, ge=1, le=365, description="Periode die Claude evalueert, in dagen")


class EvaluationIdIn(_In):
    evaluation_id: int = Field(ge=1, description="id uit evaluations")


class OrderIn(_In):
    market: str = Field(pattern=MARKET_PATTERN, description="bijv. BTC-EUR")
    side: Literal["buy", "sell"]
    amount_quote: float = Field(gt=0, description="Bedrag in euro's")
    reason: str | None = Field(
        default=None, max_length=200, description="Reden, komt in het logboek"
    )
    current_password: str | None = Field(
        default=None, max_length=1024, description="Alleen nodig terwijl de bot live handelt"
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Unieke tekst per order (bijv. een UUID). Stuur je dezelfde order nog eens "
        "met dezelfde sleutel, dan plaatst Crypie hem niet opnieuw. Gebruik hem altijd.",
    )


class StrategyIn(_In):
    strategy: Strategy = Field(description="De volledige strategie (haal eerst `strategy` op)")
    strategy_id: int | None = Field(
        default=None,
        ge=1,
        description="Welke strategie (id uit `strategies`); leeg = de eerste strategie",
    )


class StrategyAddIn(_In):
    strategy: Strategy = Field(description="De volledige regels (zoals `strategy` ze geeft)")
    markets: list[str] = Field(
        default_factory=list,
        max_length=20,
        description='Markten van deze strategie (bijv. ["ETH-EUR"]); leeg = alle gevolgde '
        "markten die geen andere strategie heeft. Een markt hoort bij één strategie tegelijk.",
    )
    budget_eur: float = Field(
        default=0,
        ge=0,
        description="Eigen budget: zoveel euro mag tegelijk in haar open posities zitten; "
        "0 = geen eigen budget (alleen de algemene limieten)",
    )
    enabled: bool = Field(default=True, description="Aan (handelt) of uit")


class StrategyUpdateIn(_In):
    strategy_id: int = Field(ge=1, description="id uit `strategies`")
    strategy: Strategy | None = Field(default=None, description="Nieuwe regels; leeg = ongewijzigd")
    markets: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Nieuwe markten; leeg = ongewijzigd, [] = alle overige",
    )
    budget_eur: float | None = Field(
        default=None, ge=0, description="Nieuw budget in euro's (0 = geen eigen budget)"
    )
    enabled: bool | None = Field(default=None, description="Aan- of uitzetten")


class StrategyIdIn(_In):
    strategy_id: int = Field(ge=1, description="id uit `strategies`")


class ProposalIn(_In):
    description: str = Field(
        min_length=5,
        max_length=DESCRIPTION_MAX,
        description="Wat de strategie moet doen, in woorden",
    )


class BacktestIn(_In):
    strategy: Strategy | None = Field(default=None, description="Leeg = de actieve strategie")
    limit: int = Field(default=500, ge=50, le=MAX_CANDLES, description="Aantal candles")
    start_eur: float = Field(
        default=1000, gt=0, le=1e9, description="Startkapitaal van de test, in euro's"
    )


class PlanIn(_In):
    goal: str = Field(default="", max_length=PLAN_MAX_LENGTH["goal"], description="Doel")
    rules: str = Field(default="", max_length=PLAN_MAX_LENGTH["rules"], description="Eigen regels")
    avoid: str = Field(
        default="", max_length=PLAN_MAX_LENGTH["avoid"], description="Wat te vermijden"
    )
    lessons: str = Field(default="", max_length=PLAN_MAX_LENGTH["lessons"], description="Lessen")


class NoteIn(_In):
    position_id: int = Field(ge=1, description="id van de positie (uit journal)")
    note: str = Field(default="", max_length=4000, description="Notitie; leeg wist de notitie")
    tags: str = Field(default="", max_length=256, description="Labels, komma-gescheiden")


class SettingsIn(_In):
    changes: dict[str, Any] = Field(
        description="Instelling -> nieuwe waarde, met de sleutels zoals `settings` ze geeft"
    )
    current_password: str | None = Field(
        default=None, max_length=1024, description="Nodig bij gevoelige instellingen"
    )


class ResetSettingsIn(_In):
    key: str | None = Field(
        default=None, max_length=64, description="Eén instelling; leeg = alle portaalwaarden"
    )
    current_password: str | None = Field(default=None, max_length=1024)


# -- tools -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    method: str
    path: str | Callable[[Any], str]
    model: type[_In] | None = None
    query: Callable[[Any], dict] | None = None
    body: Callable[[Any], dict] | None = None
    headers: Callable[[Any], dict] | None = None
    read_only: bool = False
    destructive: bool = False
    trim: Callable[[Any], Any] | None = None

    def schema(self) -> dict:
        if self.model is None:
            return {"type": "object", "properties": {}, "additionalProperties": False}
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        return schema

    def listing(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.schema(),
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


def _dump(args: Any, **kw: Any) -> dict:
    return args.model_dump(mode="json", exclude_none=True, **kw)


def _without_curve(data: Any) -> Any:
    """The backtest's SVG curve is for the page, not for a conversation."""
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != "curve"}
    return data


FIELD_KEYS = (
    "key", "label", "help", "kind", "choices", "min", "max", "ceiling", "unit", "value",
    "display", "default", "overridden", "sensitive", "depends", "warning",
)  # fmt: skip


def _compact_settings(data: Any) -> Any:
    """The settings as facts for a conversation, not as a form: per group its fields with key,
    value, default, bounds and whether a change asks the password; without the form plumbing
    (sections, placeholders, display variants) that makes /api/settings a 100 KB answer."""
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return data
    groups = []
    for group in data["groups"]:
        fields = [
            {k: f[k] for k in FIELD_KEYS if f.get(k) not in (None, "", [])}
            for section in group.get("sections", [])
            for f in section.get("fields", [])
        ]
        groups.append(
            {
                "key": group.get("key"),
                "title": group.get("title"),
                "summary": group.get("summary"),
                "needs_password": group.get("step_up_help") or None,
                "fields": fields,
            }
        )
    compact = {k: v for k, v in data.items() if k not in ("groups", "values")}
    compact["groups"] = groups
    return compact


def _password_header(args: Any) -> dict:
    return {"x-current-password": args.current_password} if args.current_password else {}


def _settings_body(args: Any) -> dict:
    body = dict(args.changes)
    if args.current_password:
        body["current_password"] = args.current_password
    return body


TOOLS: tuple[Tool, ...] = (
    # -- read --
    Tool(
        "status",
        "Status van de bot",
        "Gezondheid van de bot (handelt hij, wanneer voor het laatst, wat hapert), de harde "
        "limieten zoals ze nu staan (ruimte vandaag, blootstelling, dagverlies), posities "
        "zonder bescherming en lopende schokpauzes na zwaar nieuws (event, sinds, volgende "
        "beoordeling, uiterlijk einde, Claude's laatste oordeel), plus de schokpauzes van de "
        "laatste 30 dagen met hun duur en hoe ze eindigden. Snel, zonder Bitvavo-aanroep. "
        "Begin hier.",
        "GET",
        "/api/status",
        read_only=True,
    ),
    Tool(
        "trading_status",
        "Handelsstatus",
        "Modus (paper/live), noodstop, Claude beschikbaar, laatste cyclus, markten en per markt "
        "het laatste signaal en de dipbewaking.",
        "GET",
        "/api/trading/status",
        read_only=True,
    ),
    Tool(
        "prices",
        "Koersen",
        "De actuele koers van elke markt die de bot volgt (van Bitvavo).",
        "GET",
        "/api/prices",
        read_only=True,
    ),
    Tool(
        "candles",
        "Candles",
        "OHLCV-candles van één markt (van Bitvavo), nieuwste laatst: [tijd in ms, open, hoog, "
        "laag, sluit, volume].",
        "GET",
        lambda a: f"/api/candles/{a.market.upper()}",
        CandlesIn,
        query=lambda a: _dump(a, exclude={"market"}),
        read_only=True,
    ),
    Tool(
        "chart",
        "Koersgrafiek met acties",
        "Candles van één markt plus wat de bot in die periode deed (orders, geblokkeerde "
        "aankopen, exits), zoals op het overzicht.",
        "GET",
        lambda a: f"/api/chart/{a.market.upper()}",
        ChartIn,
        query=lambda a: _dump(a, exclude={"market"}),
        read_only=True,
    ),
    Tool(
        "portfolio_history",
        "Portefeuillewaarde",
        "De waarde van de portefeuille per candle over een periode, teruggerekend uit het saldo "
        "van nu en de orders.",
        "GET",
        "/api/portfolio/history",
        PeriodIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "balance",
        "Saldo",
        "Het saldo in euro's en per coin (de oefenportefeuille, of live het Bitvavo-account).",
        "GET",
        "/api/balance",
        read_only=True,
    ),
    Tool(
        "orders",
        "Orders",
        "De laatste orders (paper en live, of één modus): kant, bedrag, prijs, kosten, status "
        "en reden.",
        "GET",
        "/api/orders",
        OrdersIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "order",
        "Eén order",
        "Eén order op id, bijvoorbeeld om te volgen of een order met onbekende uitkomst "
        "(status pending of unknown) inmiddels is uitgevoerd.",
        "GET",
        lambda a: f"/api/orders/{a.order_id}",
        OrderIdIn,
        read_only=True,
    ),
    Tool(
        "positions",
        "Posities",
        "Open en gesloten posities met instapprijs, stop-loss, trailing stop, take-profit en "
        "resultaat.",
        "GET",
        "/api/positions",
        PositionsIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "decisions",
        "Beslissingen",
        "De laatste beslissingen van de handelscyclus: signaal, nieuws, Claude's oordeel en "
        "redenering, en wat er met de beslissing gebeurde (order, geblokkeerd door een limiet, "
        "wachten).",
        "GET",
        "/api/decisions",
        DecisionsIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "journal",
        "Trade-journaal",
        "Posities als trades van aankoop tot verkoop, met beslissingen, eigen notities en "
        "labels, en totalen (winst, verlies, netto resultaat). Filterbaar.",
        "GET",
        "/api/journal",
        JournalIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "news",
        "Nieuws",
        "Recente nieuwsberichten met Claude's beoordeling (impact, richting, score), per coin.",
        "GET",
        "/api/news",
        NewsIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "news_sentiment",
        "Marktsentiment",
        "Het sentiment per coin uit het beoordeelde nieuws, en de Fear & Greed-index.",
        "GET",
        "/api/news/sentiment",
        SentimentIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "etf_flows",
        "ETF-stromen",
        "Dagelijkse in- en uitstroom van de Amerikaanse spot-ETF's per coin: laatste "
        "handelsdag, 5 en 30 dagen, reeks en label. Zonder cryptoetf.today-sleutel alleen BTC.",
        "GET",
        "/api/news/etf-flows",
        read_only=True,
    ),
    Tool(
        "evaluations",
        "Zelfevaluaties",
        "De laatste periodieke zelfevaluaties van Claude: rapport, voorgestelde strategie, de "
        "backtest ervan en of het voorstel is toegepast.",
        "GET",
        "/api/evaluations",
        EvaluationsIn,
        query=_dump,
        read_only=True,
    ),
    Tool(
        "strategy",
        "Strategie",
        "De regels van de eerste strategie: markt, interval, indicatoren, koop- en "
        "verkoopvoorwaarden, stop-loss, trailing stop, take-profit en positiegrootte. Zie "
        "`strategies` voor alle strategieën met hun markten en budget.",
        "GET",
        "/api/strategy",
        read_only=True,
    ),
    Tool(
        "strategies",
        "Strategieën",
        "Alle strategieën van de bot: per strategie id, naam, aan/uit, de markten waarop ze "
        "handelt, haar eigen budget (en wat daarvan belegd en vrij is), open posities en de "
        "volledige regels.",
        "GET",
        "/api/strategies",
        read_only=True,
    ),
    Tool(
        "plan",
        "Tradingplan",
        "Het tradingplan van de eigenaar (doel, eigen regels, wat te vermijden, lessen) dat "
        "Claude bij elke beslissing meekrijgt.",
        "GET",
        "/api/plan",
        read_only=True,
    ),
    Tool(
        "settings",
        "Instellingen",
        "Alle instellingen zoals ze nu gelden, met per instelling de standaard, of ze aangepast "
        "is, en of een wijziging het wachtwoord vraagt.",
        "GET",
        "/api/settings",
        read_only=True,
        trim=_compact_settings,
    ),
    Tool(
        "claude_usage",
        "Claude-kosten",
        "Het verbruik van Claude deze maand tegenover het budget, per taak.",
        "GET",
        "/api/claude/usage",
        read_only=True,
    ),
    # -- act --
    Tool(
        "run_cycle",
        "Handelscyclus draaien",
        "Draai nu één handelscyclus voor alle markten (signaal, nieuws, Claude, limieten; plaatst "
        "een order als alles klopt). Geeft per markt de beslissing.",
        "POST",
        "/api/trading/cycle",
    ),
    Tool(
        "consult",
        "Claude laten beoordelen",
        "Vraag Claude nu een oordeel over één markt, ongeacht het signaal. De regels blijven "
        "gelden: zonder koopsignaal koopt hij niet.",
        "POST",
        "/api/trading/consult",
        MarketIn,
        body=lambda a: {"market": a.market.upper()},
    ),
    Tool(
        "pause",
        "Noodstop",
        "Zet de noodstop aan (geen nieuwe aankopen; exits blijven werken) of hervat het kopen.",
        "POST",
        "/api/trading/pause",
        PauseIn,
        body=_dump,
    ),
    Tool(
        "run_evaluation",
        "Zelfevaluatie starten",
        "Laat Claude de afgelopen periode evalueren en een strategievoorstel doen (kost "
        "Claude-budget, kan een minuut duren).",
        "POST",
        "/api/evaluations",
        EvaluationIn,
        query=_dump,
    ),
    Tool(
        "apply_evaluation",
        "Evaluatievoorstel toepassen",
        "Maak de voorgestelde strategie uit een evaluatie de actieve strategie.",
        "POST",
        lambda a: f"/api/evaluations/{a.evaluation_id}/apply",
        EvaluationIdIn,
        destructive=True,
    ),
    Tool(
        "refresh_news",
        "Nieuws verversen",
        "Haal nu nieuws op uit alle bronnen en laat Claude de nieuwe berichten beoordelen.",
        "POST",
        "/api/news/refresh",
    ),
    Tool(
        "place_order",
        "Handmatige order",
        "Plaats een koop- of verkooporder onder de regels van de bot (limieten, minimumbedrag). "
        "In oefenmodus gesimuleerd; live echt, en dan is current_password nodig. Geef altijd een "
        "idempotency_key mee, dan wordt een herhaalde aanroep niet nog eens geplaatst. Is de "
        "uitkomst nog onbekend (status pending of unknown), plaats hem dan niet opnieuw maar "
        "volg hem met de tool order.",
        "POST",
        "/api/orders",
        OrderIn,
        body=lambda a: _dump(a) | {"market": a.market.upper()},
        destructive=True,
    ),
    Tool(
        "set_strategy",
        "Strategie opslaan",
        "Vervang de regels van een strategie (strategy_id; leeg = de eerste). De markt moet "
        "een gevolgde markt zijn; de vorige regels blijven bewaard. Markten en budget van de "
        "strategie veranderen niet (daarvoor update_strategy).",
        "PUT",
        lambda a: f"/api/strategies/{a.strategy_id}" if a.strategy_id else "/api/strategy",
        StrategyIn,
        body=lambda a: (
            {"strategy": _dump(a)["strategy"]} if a.strategy_id else _dump(a)["strategy"]
        ),
        destructive=True,
    ),
    Tool(
        "add_strategy",
        "Strategie toevoegen",
        "Voeg een strategie toe met eigen markten en een eigen budget, naast de bestaande. "
        "Haal eerst `strategies` op om te zien welke markten nog vrij zijn.",
        "POST",
        "/api/strategies",
        StrategyAddIn,
        body=_dump,
        destructive=True,
    ),
    Tool(
        "update_strategy",
        "Strategie wijzigen",
        "Wijzig van één strategie de markten, het budget, de regels of de aan/uit-stand; "
        "alleen de meegegeven velden veranderen.",
        "PUT",
        lambda a: f"/api/strategies/{a.strategy_id}",
        StrategyUpdateIn,
        body=lambda a: _dump(a, exclude={"strategy_id"}),
        destructive=True,
    ),
    Tool(
        "remove_strategy",
        "Strategie verwijderen",
        "Verwijder een strategie (niet de laatste). Open posities in haar markten blijven "
        "bewaakt door de stops van een andere strategie.",
        "DELETE",
        lambda a: f"/api/strategies/{a.strategy_id}",
        StrategyIdIn,
        destructive=True,
    ),
    Tool(
        "propose_strategy",
        "Strategie laten voorstellen",
        "Laat Crypie's strategie-assistent een beschrijving vertalen naar een strategie (niet "
        "opgeslagen; daarna backtest of set_strategy).",
        "POST",
        "/api/strategy/propose",
        ProposalIn,
        body=_dump,
    ),
    Tool(
        "backtest",
        "Backtest",
        "Test een strategie (of de actieve) op recente candles: rendement, drawdown, trades, "
        "netto na kosten, tegenover kopen-en-houden. Rekent zoals de live bot: harde "
        "voorwaarden geven het signaal, bij een niet-gehaald zacht filter de halve inzet "
        "(trade_log noemt per trade size_pct en soft_failed). `strict` ernaast: dezelfde test "
        "met de zachte filters als harde voorwaarden.",
        "POST",
        "/api/backtest",
        BacktestIn,
        body=_dump,
        trim=_without_curve,
    ),
    Tool(
        "set_plan",
        "Tradingplan opslaan",
        "Vervang het tradingplan van de eigenaar (alle vier de velden; haal eerst `plan` op).",
        "PUT",
        "/api/plan",
        PlanIn,
        body=_dump,
        destructive=True,
    ),
    Tool(
        "journal_note",
        "Journaalnotitie",
        "Schrijf of vervang de notitie en labels bij een positie in het journaal.",
        "PUT",
        lambda a: f"/api/journal/{a.position_id}/note",
        NoteIn,
        body=lambda a: _dump(a, exclude={"position_id"}),
        destructive=True,
    ),
    Tool(
        "update_settings",
        "Instellingen wijzigen",
        "Wijzig één of meer instellingen (sleutels en waarden zoals `settings` ze geeft). "
        "Gevoelige instellingen vragen current_password; alle wijzigingen gelden direct.",
        "PATCH",
        "/api/settings",
        SettingsIn,
        body=_settings_body,
        destructive=True,
        trim=_compact_settings,
    ),
    Tool(
        "reset_settings",
        "Instellingen terugzetten",
        "Zet één instelling (key) of alle portaalwaarden terug naar de standaard uit de "
        "omgeving. Zet nooit de modus naar live.",
        "DELETE",
        lambda a: f"/api/settings/{a.key}" if a.key else "/api/settings",
        ResetSettingsIn,
        headers=_password_header,
        destructive=True,
        trim=_compact_settings,
    ),
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# -- calling the API in-process ----------------------------------------------------------------


def _api_client(request: Request) -> httpx.AsyncClient:
    """A client that calls this app's own API, as the MCP client would over the network: the
    same bearer token, the same client address (for the step-up throttle), no compression."""
    headers = {"accept": "application/json", "accept-encoding": "identity"}
    authorization = request.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization
    client = tuple(request.client) if request.client else ("127.0.0.1", 0)
    transport = httpx.ASGITransport(app=request.app, client=client)
    return httpx.AsyncClient(
        transport=transport, base_url="http://crypie.mcp", headers=headers, timeout=None
    )


def _text(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


_NEXT_LINK = re.compile(r'<([^>]*)>\s*;\s*rel="?next"?')


def next_cursor(response: httpx.Response) -> str | None:
    """The cursor of the next page, from the Link header of a paged list (RFC 8288)."""
    match = _NEXT_LINK.search(response.headers.get("link", ""))
    if not match:
        return None
    values = parse_qs(urlsplit(match.group(1)).query).get("cursor")
    return values[0] if values else None


def _tool_result(tool: Tool, response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = response.text
    if response.status_code >= 400:
        detail = data.get("detail", data) if isinstance(data, dict) else data
        code = f" [{data['code']}]" if isinstance(data, dict) and data.get("code") else ""
        text = f"Crypie weigerde dit ({response.status_code}{code}): {_text(detail)}"
        return {"content": [{"type": "text", "text": text}], "isError": True}
    if tool.trim is not None:
        data = tool.trim(data)
    text = _text(data)
    cursor = next_cursor(response)
    if cursor:
        text += f'\n\nEr is meer: roep {tool.name} opnieuw aan met cursor="{cursor}".'
    result: dict = {"content": [{"type": "text", "text": text}]}
    if isinstance(data, dict):
        result["structuredContent"] = {**data, "next_cursor": cursor} if cursor else data
    return result


async def call_tool(request: Request, tool: Tool, args: Any) -> dict:
    path = tool.path(args) if callable(tool.path) else tool.path
    params = tool.query(args) if tool.query else None
    body = tool.body(args) if tool.body else None
    headers = tool.headers(args) if tool.headers else None
    async with _api_client(request) as api:
        response = await api.request(tool.method, path, params=params, json=body, headers=headers)
    return _tool_result(tool, response)


# -- JSON-RPC over HTTP ----------------------------------------------------------------------


def _ok(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _fail(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _validation_data(exc: ValidationError) -> list[dict]:
    return [
        {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
    ]


async def _tools_call(request: Request, msg_id: Any, params: dict) -> dict:
    name = params.get("name")
    tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return _fail(msg_id, INVALID_PARAMS, f"Onbekende tool: {name!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _fail(msg_id, INVALID_PARAMS, "arguments moet een object zijn")
    args = None
    if tool.model is not None:
        try:
            args = tool.model.model_validate(arguments)
        except ValidationError as exc:
            return _fail(
                msg_id, INVALID_PARAMS, f"Ongeldige invoer voor {name}", _validation_data(exc)
            )
    elif arguments:
        return _fail(msg_id, INVALID_PARAMS, f"{name} neemt geen argumenten")
    try:
        result = await call_tool(request, tool, args)
    except Exception:  # noqa: BLE001 - the failure must reach Claude, not only the log
        log.exception("MCP tool %s failed", name)
        text = f"{name} is mislukt door een fout in Crypie; de serverlog zegt meer."
        return _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})
    return _ok(msg_id, result)


def _initialize(request: Request, msg_id: Any, params: dict) -> dict:
    asked = params.get("protocolVersion")
    version = asked if asked in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
    return _ok(
        msg_id,
        {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "Crypie",
                "version": getattr(request.app, "version", "0"),
            },
            "instructions": INSTRUCTIONS,
        },
    )


async def _handle(request: Request, msg: Any) -> dict | None:
    """One JSON-RPC message; None for a notification or a response (nothing to send back)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _fail(None, INVALID_REQUEST, "Geen JSON-RPC 2.0-bericht")
    msg_id = msg.get("id")
    method = msg.get("method")
    if method is None:
        return None  # a response from the client; this server never asks it anything
    if not isinstance(method, str):
        return _fail(msg_id, INVALID_REQUEST, "method moet tekst zijn")
    if "id" not in msg:
        return None  # notifications (initialized, cancelled, ...) need no answer
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _fail(msg_id, INVALID_PARAMS, "params moet een object zijn")
    if method == "initialize":
        return _initialize(request, msg_id, params)
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": [t.listing() for t in TOOLS]})
    if method == "tools/call":
        return await _tools_call(request, msg_id, params)
    return _fail(msg_id, METHOD_NOT_FOUND, f"Onbekende methode: {method}")


def _json(body: Any, status: int = 200, **headers: str) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **headers})


@router.post(MCP_PATH, include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    version = request.headers.get(VERSION_HEADER)
    if version is not None and version not in PROTOCOL_VERSIONS:
        return _json({"detail": f"MCP-Protocol-Version {version} wordt niet ondersteund"}, 400)
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return _json(_fail(None, PARSE_ERROR, "Ongeldige JSON"))
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    if not messages:
        return _json(_fail(None, INVALID_REQUEST, "Lege batch"))
    answers = [a for a in [await _handle(request, m) for m in messages] if a is not None]
    if not answers:
        return Response(status_code=202, headers={"Cache-Control": "no-store"})
    return _json(answers if batch else answers[0])


@router.get(MCP_PATH, include_in_schema=False)
@router.delete(MCP_PATH, include_in_schema=False)
async def mcp_other() -> Response:
    """No server-initiated stream and no session to end: only POST."""
    return _json({"detail": "Alleen POST: dit MCP-endpoint is stateless"}, 405, Allow="POST")
