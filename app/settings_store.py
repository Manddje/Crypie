"""Settings that can be changed in the portal.

Values saved here live in the ``settings`` table (keys ``config:<name>``) and win over the
environment; the environment (Coolify / .env) is only the fallback and supplies the
defaults. Secrets (API keys, tokens, the database URL) and the LIVE_TRADING_ACK safeguard
are deliberately *not* editable here: they stay in the environment.
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.budget import PRICES
from app.config import Settings, get_settings
from app.db import Setting
from app.news.sources import validate_feed_url
from app.numbers import AmbiguousNumber, ambiguous_nl, split_decimal, strip_spaces
from app.trading.events import validate_calendar_lines
from app.web import num

log = logging.getLogger(__name__)

CONFIG_PREFIX = "config:"
MARKET_RE = re.compile(r"^[A-Z0-9]{2,10}-EUR$")

# Upper bounds on list settings and on any raw value, so one form post cannot store
# thousands of markets or feeds (every one costs API calls each cycle).
MAX_MARKETS = 20
MAX_FEEDS = 30
MAX_CALENDAR_LINES = 200
MAX_URL_LENGTH = 500
MAX_RAW_LENGTH = 20_000

# Limits with a hard ceiling that only the environment can set: setting key -> ceiling field.
CEILINGS: dict[str, str] = {
    "max_trade_eur": "max_trade_eur_ceiling",
    "max_exposure_pct": "max_exposure_pct_ceiling",
    "max_trades_per_day": "max_trades_per_day_ceiling",
}

# The blocks of the settings page that save a sensitive setting; each one is a form of its own
# with its own Opslaan button and current-password field. Most are one settings group (Spec.group);
# Bescherming is one part of the group trading (PART_GROUPS).
GROUPS: dict[str, tuple[str, str]] = {
    "trading": ("Handelen", ""),
    "bescherming": (
        "Bescherming",
        "Wat open posities beschermt en wanneer de bot tijdelijk niet koopt.",
    ),
    "limits": (
        "Harde limieten",
        "Grenzen die altijd gelden, wat Claude ook adviseert.",
    ),
    "costs": (
        "Kosten",
        "Geen limieten: hiermee rekent de bot wat een order kost.",
    ),
    "claude": ("Claude", ""),
    "news": ("Nieuws", ""),
}
# Blocks that are one part of a settings group (UX-57): block -> (group, part). The settings keep
# their group (the API and the environment list them under Handelen), but the page saves them in
# a block of their own, so saving a protection setting does not send the mode and the markets.
PART_GROUPS: dict[str, tuple[str, str]] = {
    "bescherming": ("trading", "bescherming"),
}
# The paper portfolio's start amount is saved together with its reset (its own card), not with
# a block above: it only matters for a new or reset paper portfolio.
PAPER_GROUP = "paper"

# Parts of a block, in order: group -> [(section, title, help)]. A group without an entry is one
# part without a sub-heading. Parts that are a block of their own (PART_GROUPS, OWN_FORM_PARTS)
# are left out of their group's block.
SECTIONS: dict[str, list[tuple[str, str, str]]] = {
    "trading": [
        ("modus", "Modus en markten", ""),
        ("kopen", "Kopen", ""),
        ("bescherming", "Bescherming", ""),
        ("weergave", "Weergave", ""),
    ],
    "news": [
        ("ophalen", "Ophalen en beoordelen", ""),
        ("bronnen", "Bronnen", ""),
    ],
}

# Parts with a block (a form and an Opslaan button) of their own on the page, apart from the
# block they belong to (UX-57): what a page shows is saved without the mode and the markets.
# Only parts without a sensitive setting, so without a password field (a part with one is a
# block in GROUPS, see PART_GROUPS). part -> (group, title, description).
OWN_FORM_PARTS: dict[str, tuple[str, str, str]] = {
    "weergave": (
        "trading",
        "Weergave",
        "Alleen wat je ziet; de bot handelt er niet anders door.",
    ),
}
# Sections of a group that are not in that group's own block.
_SPLIT_SECTIONS = {part for _, part in PART_GROUPS.values()} | set(OWN_FORM_PARTS)

# The line under a folded block's title (UX-56): the values that matter most, as
# (setting, wording with {} for its value).
SUMMARY: dict[str, tuple[tuple[str, str], ...]] = {
    "trading": (
        ("trading_mode", "{}"),
        ("markets", "{}"),
        ("trade_interval_min", "elke {}"),
        ("trading_enabled", "automatisch handelen {}"),
    ),
    "bescherming": (
        ("exit_check_interval_min", "stop-loss en take-profit elke {}"),
        ("risk_off_enabled", "risk-off {}"),
        ("shock_pause_hours", "schok-pauze {}"),
    ),
    "limits": (
        ("max_trade_eur", "max. {} per order"),
        ("max_exposure_pct", "max. {} in crypto"),
        ("daily_loss_limit_pct", "max. {} dagverlies"),
        ("max_trades_per_day", "max. {} per dag"),
    ),
    "costs": (("trading_fee_pct", "{} per order"), ("backtest_fee_pct", "{} in backtests")),
    "claude": (
        ("claude_monthly_budget_usd", "{} per maand"),
        ("evaluation_interval_hours", "evaluatie elke {}"),
    ),
    "news": (
        ("news_enabled", "automatisch ophalen {}"),
        ("news_fetch_interval_min", "elke {}"),
        ("news_rss_feeds", "{}"),
    ),
    "weergave": (("decision_history_days", "beslissingen van de laatste {}"),),
}


def block_summary(block: str, fields: list[dict]) -> str:
    """'Oefenmodus · BTC-EUR, ETH-EUR · elke 60 minuten · automatisch handelen aan'."""
    by_key = {f["key"]: f for f in fields}
    return " · ".join(
        wording.format(by_key[key]["display"])
        for key, wording in SUMMARY.get(block, ())
        if key in by_key
    )


# How a hard limit becomes looser: "higher" (a bigger number allows more), "lower" (a smaller
# number allows more) or "capital" (more money, and 0 means the whole account). A save that
# makes one more than twice as loose in one go asks for an extra confirmation in the portal.
LOOSEN_RULES: dict[str, str] = {
    "max_trade_eur": "higher",
    "live_capital_eur": "capital",
    "max_exposure_pct": "higher",
    "daily_loss_limit_pct": "higher",
    "max_trades_per_day": "higher",
    "cooldown_min_after_stop": "lower",
}


@dataclass(frozen=True)
class Spec:
    """One editable setting: how to show it, parse it and check it."""

    key: str
    group: str
    label: str
    help: str = ""
    kind: str = "str"  # bool | int | float | str | choice | model | markets | lines | events
    choices: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    unit: str = ""
    restart: bool = False  # True when a change only takes effect after a restart
    extra: dict[str, Any] = field(default_factory=dict)
    section: str = ""  # part of the block (SECTIONS)
    # The switch this field belongs to: it only does something while that one is on (a bool)
    # or above 0 (a number). The page dims it then; the value is kept and still saved.
    depends: str = ""
    placeholder: str = ""

    @property
    def env_name(self) -> str:
        return self.key.upper()


MODEL_CHOICES = tuple((m, m) for m in PRICES)
KNOWN_MODELS: tuple[str, ...] = tuple(PRICES)

SPECS: tuple[Spec, ...] = (
    # -- trading: modus en markten --
    Spec(
        "trading_mode",
        "trading",
        "Modus",
        "Oefenmodus handelt met fictief geld, Live met echt geld op Bitvavo.",
        kind="choice",
        choices=(("paper", "Oefenmodus"), ("live", "Live")),
        section="modus",
    ),
    Spec(
        "trading_enabled",
        "trading",
        "Bot automatisch laten handelen",
        "Uit: geen geplande cycli; stop-loss, trailing stop en take-profit blijven werken. "
        "Alleen kopen stoppen? Gebruik de noodstop.",
        kind="bool",
        section="modus",
    ),
    Spec(
        "markets",
        "trading",
        "Markten",
        "Gescheiden door komma's, in Bitvavo-notatie.",
        kind="markets",
        section="modus",
        placeholder="BTC-EUR, ETH-EUR",
    ),
    Spec(
        "trade_interval_min",
        "trading",
        "Interval tussen cycli",
        "Hoe vaak de bot per markt naar koers, indicatoren en nieuws kijkt.",
        kind="int",
        min=1,
        max=1440,
        unit="minuten",
        section="modus",
    ),
    # -- trading: kopen --
    Spec(
        "claude_unavailable_mode",
        "trading",
        "Als Claude niet beschikbaar is",
        "Bij een koopsignaal zonder werkende Claude (geen sleutel, budget op, storing). "
        "Verkoopsignalen worden altijd gevolgd.",
        kind="choice",
        choices=(
            ("hold", "Afwachten, niet kopen"),
            ("indicators", "Het strategiesignaal volgen (kopen met half bedrag)"),
        ),
        section="kopen",
    ),
    Spec(
        "trend_context_enabled",
        "trading",
        "Lange-termijntrend (PlanB)",
        "Het 200-weeks gemiddelde en de maand-RSI per coin, als context voor Claude; geen signaal.",
        kind="bool",
        section="kopen",
    ),
    Spec(
        "watch_interval_min",
        "trading",
        "Dipbewaking: controle elke",
        "Zit een markt in een dip maar is de draai er nog niet, dan kijkt de bot zo vaak "
        "opnieuw. 0 = uit.",
        kind="int",
        min=0,
        max=60,
        unit="minuten",
        section="kopen",
    ),
    Spec(
        "watch_max_min",
        "trading",
        "Dipbewaking: maximaal",
        "Daarna wacht de bot weer op de gewone cyclus.",
        kind="int",
        min=5,
        max=1440,
        unit="minuten",
        section="kopen",
        depends="watch_interval_min",
    ),
    # -- trading: bescherming --
    Spec(
        "exit_check_interval_min",
        "trading",
        "Stop-loss en take-profit: controle elke",
        "Loopt altijd door, voor alle open posities: ook met automatisch handelen uit, de "
        "noodstop aan of een markt buiten de lijst.",
        kind="int",
        min=1,
        max=60,
        unit="minuten",
        section="bescherming",
    ),
    Spec(
        "risk_off_enabled",
        "trading",
        "Risk-off rond agendamomenten",
        "Geen nieuwe aankopen rond agendamomenten en na een nieuwsschok; verkopen en de "
        "stop-loss blijven werken.",
        kind="bool",
        section="bescherming",
    ),
    Spec(
        "risk_off_before_min",
        "trading",
        "Risk-off: vooraf",
        "",
        kind="int",
        min=0,
        max=720,
        unit="minuten",
        section="bescherming",
        depends="risk_off_enabled",
    ),
    Spec(
        "risk_off_after_min",
        "trading",
        "Risk-off: achteraf",
        "",
        kind="int",
        min=0,
        max=1440,
        unit="minuten",
        section="bescherming",
        depends="risk_off_enabled",
    ),
    Spec(
        "shock_pause_hours",
        "trading",
        "Schok-pauze",
        "Na een nieuwsschok (impact groot, sterk negatief) koopt de bot zo lang niet in de "
        "geraakte coins. 0 = uit.",
        kind="int",
        min=0,
        max=72,
        unit="uur",
        section="bescherming",
        depends="risk_off_enabled",
    ),
    Spec(
        "event_calendar",
        "trading",
        "Agenda",
        "Eén moment per regel: datum, tijd in UTC, omschrijving. Standaard: "
        "FOMC-rentebesluiten en Amerikaanse CPI-cijfers t/m 2027.",
        kind="events",
        section="bescherming",
        depends="risk_off_enabled",
        placeholder="2026-12-09 19:00 FOMC-rentebesluit",
    ),
    # -- trading: weergave --
    Spec(
        "decision_history_days",
        "trading",
        "Beslissingen tonen van de laatste",
        "Zo veel dagen aan cycli toont de pagina Handelen.",
        kind="int",
        min=1,
        max=365,
        unit="dagen",
        section="weergave",
    ),
    # -- paper portfolio (saved with its reset) --
    Spec(
        "paper_start_eur",
        PAPER_GROUP,
        "Startbedrag",
        "Opgeslagen met de reset; geldt vanaf dan.",
        kind="float",
        min=10,
        max=10_000_000,
        unit="€",
    ),
    # -- limits --
    Spec(
        "max_trade_eur",
        "limits",
        "Maximum per order",
        "Geldt voor elke order: van Claude, van de regels en van jou.",
        kind="float",
        min=5,
        max=1_000_000,
        unit="€",
    ),
    Spec(
        "min_trade_eur",
        "limits",
        "Minimum per order",
        "Kleinere orders worden niet geplaatst; Bitvavo accepteert minimaal € 5.",
        kind="float",
        min=5,
        max=100_000,
        unit="€",
    ),
    Spec(
        "live_capital_eur",
        "limits",
        "Kapitaal voor de bot (live)",
        "Live rekenen de limieten voor crypto en dagverlies met dit bedrag en de posities "
        "van de bot, niet met je hele account. 0 = het hele account.",
        kind="float",
        min=0,
        max=10_000_000,
        unit="€",
    ),
    Spec(
        "max_exposure_pct",
        "limits",
        "Maximaal in crypto",
        "Deel van de totale portefeuille dat in coins mag zitten.",
        kind="float",
        min=0,
        max=100,
        unit="%",
    ),
    Spec(
        "daily_loss_limit_pct",
        "limits",
        "Dagverlieslimiet",
        "Daarna koopt de bot vandaag niets meer. Telt gerealiseerd verlies plus verlies op "
        "open posities; er wordt niets verkocht. Nieuwe dag om 00:00 UTC.",
        kind="float",
        min=0.1,
        max=100,
        unit="%",
    ),
    Spec(
        "max_trades_per_day",
        "limits",
        "Maximum aantal orders per dag",
        "Elke uitgevoerde koop en verkoop telt, ook handmatig en door een stop. Daarna "
        "koopt de bot niets tot 00:00 UTC; verkopen kan altijd. 0 = niet kopen.",
        kind="int",
        min=0,
        max=200,
        unit="orders",
    ),
    Spec(
        "cooldown_min_after_stop",
        "limits",
        "Afkoelperiode na stop-loss",
        "Zo lang koopt de bot niet opnieuw in een markt na een stop-loss.",
        kind="int",
        min=0,
        max=10_080,
        unit="minuten",
    ),
    # -- costs --
    Spec(
        "trading_fee_pct",
        "costs",
        "Handelskosten per order",
        "Het taker-tarief van Bitvavo, bij kopen én verkopen. Oefenorders betalen het en "
        "netto resultaten rekenen ermee; live geldt je echte tarief.",
        kind="float",
        min=0,
        max=5,
        unit="%",
    ),
    Spec(
        "backtest_fee_pct",
        "costs",
        "Handelskosten in backtests",
        "Wat backtests op de strategiepagina per order rekenen.",
        kind="float",
        min=0,
        max=5,
        unit="%",
    ),
    # -- claude --
    Spec(
        "claude_monthly_budget_usd",
        "claude",
        "Maandbudget",
        "Bij 90% waarschuwt het overzicht, bij 100% stoppen alle Claude-aanroepen tot de "
        "volgende maand.",
        kind="float",
        min=0,
        max=100_000,
        unit="$",
    ),
    Spec(
        "claude_model_decision",
        "claude",
        "Model voor handelsbeslissingen",
        "Elke cyclus met een signaal.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "claude_model_news",
        "claude",
        "Model voor nieuwsbeoordeling",
        "Veel berichten per dag; Haiku houdt dat goedkoop.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "claude_model_strategy",
        "claude",
        "Model voor strategievoorstellen",
        "Alleen als je op Strategie een voorstel vraagt.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "claude_model_evaluation",
        "claude",
        "Model voor de evaluatie",
        "Zeldzaam, dus het sterkste model is hier betaalbaar.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "evaluation_interval_hours",
        "claude",
        "Evaluatie-interval",
        "Hoe vaak Claude terugkijkt en eventueel een strategie voorstelt. 0 = uit.",
        kind="int",
        min=0,
        max=8760,
        unit="uur",
    ),
    # -- news: ophalen --
    Spec(
        "news_enabled",
        "news",
        "Nieuws automatisch ophalen",
        "Uit: alleen ophalen met 'Nu verversen' op de nieuwspagina.",
        kind="bool",
        section="ophalen",
    ),
    Spec(
        "news_fetch_interval_min",
        "news",
        "Interval nieuws ophalen",
        "Hoe vaak alle bronnen gelezen en beoordeeld worden.",
        kind="int",
        min=1,
        max=1440,
        unit="minuten",
        section="ophalen",
        depends="news_enabled",
    ),
    Spec(
        "news_max_age_hours",
        "news",
        "Berichten ouder dan dit negeren",
        "Oudere berichten worden niet beoordeeld en tellen niet mee.",
        kind="int",
        min=1,
        max=720,
        unit="uur",
        section="ophalen",
    ),
    Spec(
        "news_batch_size",
        "news",
        "Berichten per Claude-aanroep",
        "Groter is goedkoper; te groot maakt de beoordeling slordiger.",
        kind="int",
        min=1,
        max=50,
        unit="berichten",
        section="ophalen",
    ),
    # -- news: bronnen --
    Spec(
        "news_rss_feeds",
        "news",
        "RSS-feeds",
        "Eén https-adres per regel. Standaard: acht cryptonieuwssites, zoals CoinDesk en "
        "Cointelegraph.",
        kind="lines",
        section="bronnen",
        placeholder="https://www.coindesk.com/arc/outboundfeeds/rss/",
    ),
    Spec(
        "cryptocv_enabled",
        "news",
        "cryptocurrency.cv",
        "Gratis verzamelaar van 300+ bronnen, per coin getagd.",
        kind="bool",
        section="bronnen",
    ),
    Spec(
        "google_news_enabled",
        "news",
        "Google News per coin",
        "Gratis zoekfeed per coin over de laatste 24 uur.",
        kind="bool",
        section="bronnen",
    ),
    Spec(
        "fear_greed_enabled",
        "news",
        "Fear & Greed-index",
        "Angst/hebzucht-score van de markt (alternative.me), als context voor Claude.",
        kind="bool",
        section="bronnen",
    ),
    Spec(
        "gdelt_enabled",
        "news",
        "Wereldnieuws via GDELT",
        "Gratis, maar hoogstens één verzoek per 5 seconden.",
        kind="bool",
        section="bronnen",
    ),
    Spec(
        "gdelt_query",
        "news",
        "GDELT-zoekopdracht",
        "Zoektermen in de GDELT-syntaxis.",
        kind="str",
        section="bronnen",
        depends="gdelt_enabled",
    ),
    Spec(
        "cryptopanic_plan",
        "news",
        "CryptoPanic-abonnement",
        "",
        kind="choice",
        choices=(
            ("growth", "Growth"),
            ("growth_weekly", "Growth (weekly)"),
            ("enterprise", "Enterprise"),
        ),
        section="bronnen",
    ),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}
EDITABLE_KEYS = tuple(SPEC_BY_KEY)

# Changing (or resetting) these asks for the current password again: they decide whether real
# money is traded, how much, in which markets, how well open positions are protected (risk-off
# window, shock pause, exit checks), and which outside text reaches Claude.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "trading_mode",
        "trading_enabled",
        "markets",
        "exit_check_interval_min",
        "claude_unavailable_mode",
        "risk_off_enabled",
        "risk_off_before_min",
        "risk_off_after_min",
        "event_calendar",
        "shock_pause_hours",
        "news_rss_feeds",
        "claude_monthly_budget_usd",
        # The fee sits in its own block (it is no hard limit), but it still decides what paper
        # orders cost and how results are counted, so it keeps asking for the password.
        "trading_fee_pct",
    }
    | {s.key for s in SPECS if s.group == "limits"}
)


def sensitive_changes(old: Settings, new: Settings) -> list[str]:
    """The sensitive settings whose effective value differs between ``old`` and ``new``."""
    return [k for k in EDITABLE_KEYS if k in SENSITIVE_KEYS and getattr(old, k) != getattr(new, k)]


def group_has_sensitive(group: str) -> bool:
    """Whether the block ``group`` (a key of GROUPS) saves a sensitive setting."""
    return any(key in SENSITIVE_KEYS for key in keys_in_form(group))


# What the current-password field of each block is needed for (the password row's help text).
STEP_UP_HELP: dict[str, str] = {
    "trading": "Nodig voor elk veld met een slotje: de modus, automatisch handelen aan/uit, "
    "de markten en het gedrag zonder Claude.",
    "bescherming": "Nodig voor elk veld met een slotje: de exit-controle en alles van risk-off.",
    "limits": "Nodig als je een harde limiet wijzigt of terugzet: elk veld in dit blok.",
    "costs": "Nodig als je de handelskosten per order wijzigt of terugzet: oefenorders betalen "
    "ze en de netto resultaten rekenen ermee.",
    "claude": "Nodig als je het maandbudget wijzigt of terugzet.",
    "news": "Nodig als je de RSS-feeds wijzigt of terugzet.",
}


class SettingsError(ValueError):
    """Validation failed; ``errors`` maps setting key -> Dutch message."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


# -- storage -----------------------------------------------------------------------------


def load_overrides(session: Session) -> dict[str, Any]:
    """Every value saved in the portal, decoded from JSON. Unknown keys are ignored."""
    rows = session.scalars(select(Setting).where(Setting.key.like(f"{CONFIG_PREFIX}%"))).all()
    out: dict[str, Any] = {}
    for row in rows:
        key = row.key.removeprefix(CONFIG_PREFIX)
        if key not in SPEC_BY_KEY:
            continue
        try:
            out[key] = json.loads(row.value)
        except ValueError:
            log.warning("ignoring unreadable setting %s", row.key)
    return out


def effective_settings(session: Session, env: Settings | None = None) -> Settings:
    """Environment settings with the portal's overrides applied and validated.

    A stored value that no longer validates (for example after an upgrade) is dropped
    with a warning rather than taking the app down.
    """
    env = env or get_settings()
    return _effective_from(env, load_overrides(session))


def preview_reset(
    session: Session, keys: list[str] | None, env: Settings | None = None
) -> Settings:
    """The settings as they would be after forgetting ``keys`` (None: every stored value),
    without touching the database."""
    env = env or get_settings()
    stored = load_overrides(session)
    remaining = {} if keys is None else {k: v for k, v in stored.items() if k not in keys}
    return _effective_from(env, remaining)


def _effective_from(env: Settings, overrides: dict[str, Any]) -> Settings:
    overrides = dict(overrides)
    while True:
        try:
            return _build(env, overrides)
        except ValidationError as exc:
            bad = {str(e["loc"][0]) for e in exc.errors() if e["loc"]}
            if not bad or not bad & overrides.keys():
                raise
            for key in bad:
                log.warning("ignoring invalid stored setting %s: %r", key, overrides.get(key))
                overrides.pop(key, None)


def _build(env: Settings, overrides: dict[str, Any]) -> Settings:
    # Init kwargs beat the environment in pydantic-settings, which is exactly the order we
    # want: portal > environment > built-in default. Validators run on the stored values.
    return apply_ceilings(Settings(**overrides))


def apply_ceilings(settings: Settings) -> Settings:
    """Clamp the limits to their environment ceilings.

    The portal refuses values above a ceiling; this catches what was stored before a ceiling
    existed or was lowered, and an environment value above its own ceiling.
    """
    update: dict[str, Any] = {}
    for key, ceiling_key in CEILINGS.items():
        value, ceiling = getattr(settings, key), getattr(settings, ceiling_key)
        if value > ceiling:
            log.warning(
                "%s=%r is above %s=%r; using the ceiling", key, value, ceiling_key.upper(), ceiling
            )
            update[key] = type(value)(ceiling)
    max_trade = update.get("max_trade_eur")
    if max_trade is not None and settings.min_trade_eur > max_trade:
        # A low ceiling must not leave an unsatisfiable minimum behind (and make every portal
        # save fail on min_trade_eur): the minimum follows the clamped maximum.
        log.warning(
            "min_trade_eur=%r is above the clamped max_trade_eur=%r; using the maximum",
            settings.min_trade_eur,
            max_trade,
        )
        update["min_trade_eur"] = float(max_trade)
    return settings.model_copy(update=update) if update else settings


def ceiling_of(settings: Settings, key: str) -> float | None:
    """The environment ceiling for this setting, or None when it has none."""
    ceiling_key = CEILINGS.get(key)
    return getattr(settings, ceiling_key) if ceiling_key else None


def save_overrides(session: Session, values: dict[str, Any], env: Settings | None = None) -> None:
    """Persist validated values. A value equal to the environment default is removed, so the
    table only ever holds real deviations from the environment."""
    env = env or get_settings()
    for key, value in values.items():
        spec = SPEC_BY_KEY[key]
        row_key = CONFIG_PREFIX + key
        row = session.get(Setting, row_key)
        if value == getattr(env, spec.key):
            if row is not None:
                session.delete(row)
            continue
        payload = json.dumps(value)
        if row is None:
            session.add(Setting(key=row_key, value=payload))
        else:
            row.value = payload
    session.commit()


def reset_overrides(session: Session, keys: list[str] | None = None) -> int:
    """Delete stored values (all of them, or only ``keys``). Returns how many were removed."""
    rows = session.scalars(select(Setting).where(Setting.key.like(f"{CONFIG_PREFIX}%"))).all()
    removed = 0
    for row in rows:
        if keys is None or row.key.removeprefix(CONFIG_PREFIX) in keys:
            session.delete(row)
            removed += 1
    session.commit()
    return removed


def keys_in_group(group: str) -> list[str]:
    return [s.key for s in SPECS if s.group == group]


def keys_in_form(group: str, part: str = "") -> list[str]:
    """The settings one form of the page saves: a block of GROUPS (a whole settings group, or
    the part PART_GROUPS names), a part with its own form (OWN_FORM_PARTS), or nothing."""
    if part:
        if OWN_FORM_PARTS.get(part, ("",))[0] != group:
            return []
        return [s.key for s in SPECS if s.group == group and s.section == part]
    if group in PART_GROUPS:
        owner, section = PART_GROUPS[group]
        return [s.key for s in SPECS if s.group == owner and s.section == section]
    return [s.key for s in SPECS if s.group == group and s.section not in _SPLIT_SECTIONS]


# -- parsing & validation ---------------------------------------------------------------


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "on", "yes", "ja", "aan"):
        return True
    if text in ("0", "false", "off", "no", "nee", "uit", ""):
        return False
    raise ValueError("kies aan of uit")


# A typed number: optional sign, digits, and at most one decimal separator (comma or point).


def parse_decimal(raw: Any) -> float:
    """A number as typed in a settings field: '2,5', '2.5', '1000' or '-3' (a JSON number
    passes as is). The decimal comma of a Dutch keyboard is fine, but a value whose separator
    could also group thousands ('1.000', '2,500', '1.000,50') is refused rather than guessed,
    and so is anything that is not a plain number ('1e3', 'inf', 'nan'). The same rule as the
    order amount (app/numbers.py)."""
    if isinstance(raw, bool):
        raise ValueError("getal verwacht")
    if isinstance(raw, int | float):
        value = float(raw)
    else:
        text = strip_spaces(raw)
        if text == "":
            raise ValueError("vul een getal in")
        try:
            sign, whole, _, frac = split_decimal(text)
        except AmbiguousNumber:
            raise ValueError(ambiguous_nl(text, "1000 of 2,5")) from None
        value = float(f"{sign}{whole or '0'}.{frac or '0'}")
    if not math.isfinite(value):
        # NaN passes every min/max check (comparisons are False) and switches limits off.
        raise ValueError("geen geldig getal")
    return value


def _parse_number(spec: Spec, raw: Any) -> float | int:
    value: float | int = parse_decimal(raw)
    if spec.kind == "int":
        if value != int(value):
            raise ValueError("heel getal verwacht")
        value = int(value)
    if spec.min is not None and value < spec.min:
        raise ValueError(f"minimaal {display_value(spec, spec.min)}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"maximaal {display_value(spec, spec.max)}")
    return value


def _parse_markets(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    markets: list[str] = []
    for part in parts:
        m = str(part).strip().upper()
        if not m:
            continue
        if not MARKET_RE.fullmatch(m):
            raise ValueError(f"{m} is geen euromarkt in Bitvavo-notatie (bijvoorbeeld BTC-EUR)")
        if m not in markets:
            markets.append(m)
    if not markets:
        raise ValueError("kies minstens één markt")
    if len(markets) > MAX_MARKETS:
        raise ValueError(f"maximaal {MAX_MARKETS} markten")
    return markets


def _feed_problem(url: str) -> str | None:
    """Why this feed address is refused, or None. Only https to a named public host: anyone on
    the network path can rewrite a plain-http feed, and its text reaches the sentiment and
    Claude."""
    shown = url[:60]
    if len(url) > MAX_URL_LENGTH:
        return f"'{shown}…' is te lang (maximaal {MAX_URL_LENGTH} tekens)"
    if re.match(r"^http://\S+$", url, re.IGNORECASE):
        return f"'{shown}': gebruik https:// (een http-feed kan onderweg worden aangepast)"
    if not re.match(r"^https://\S+$", url, re.IGNORECASE):
        return f"'{shown}' is geen geldig adres (begin met https://)"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return f"'{shown}' is geen geldig adres"
    if parts.scheme.lower() != "https" or not host:
        return f"'{shown}' is geen geldig adres (begin met https://)"
    if parts.username is not None or parts.password is not None:
        return f"'{shown}': een adres met inloggegevens is niet toegestaan"
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return f"'{shown}': localhost is niet toegestaan"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return f"'{shown}': gebruik een domeinnaam, geen IP-adres"
    if "." not in host or re.fullmatch(r"[0-9.]+|0x[0-9a-f.x]+", host):
        # Single-label names resolve on the local network; numeric forms like 2130706433 or
        # 0x7f.1 are IP addresses in disguise.
        return f"'{shown}': gebruik een volledige domeinnaam"
    try:
        # The news fetcher's own check (also .local/.internal names and control characters),
        # so the portal refuses what the fetcher would skip anyway.
        validate_feed_url(url)
    except ValueError as exc:
        return f"'{shown}': {exc}"
    return None


def _parse_lines(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, list) else re.split(r"[\r\n,]+", str(raw))
    urls: list[str] = []
    for part in parts:
        url = str(part).strip()
        if not url:
            continue
        problem = _feed_problem(url)
        if problem:
            raise ValueError(problem)
        if url not in urls:
            urls.append(url)
    if not urls:
        raise ValueError("vul minstens één feed in")
    if len(urls) > MAX_FEEDS:
        raise ValueError(f"maximaal {MAX_FEEDS} feeds")
    return urls


def _parse_events(raw: Any) -> list[str]:
    lines = raw if isinstance(raw, list) else re.split(r"[\r\n;]+", str(raw))
    if sum(1 for line in lines if str(line).strip()) > MAX_CALENDAR_LINES:
        raise ValueError(f"maximaal {MAX_CALENDAR_LINES} regels")
    return validate_calendar_lines(raw)


def parse_value(spec: Spec, raw: Any) -> Any:
    """Turn a form/JSON value into the typed value the Settings model expects."""
    if isinstance(raw, list):
        if len(raw) > 1000 or any(len(str(v)) > MAX_URL_LENGTH for v in raw):
            raise ValueError("te lang")
    elif len(str(raw)) > MAX_RAW_LENGTH:
        raise ValueError("te lang")
    if spec.kind == "bool":
        return _parse_bool(raw)
    if spec.kind in ("int", "float"):
        return _parse_number(spec, raw)
    if spec.kind == "choice":
        value = str(raw).strip()
        if value not in {c for c, _ in spec.choices}:
            raise ValueError("ongeldige keuze")
        return value
    if spec.kind == "model":
        value = str(raw).strip()
        if not re.match(r"^claude-[a-z0-9.-]+$", value):
            raise ValueError("een modelnaam begint met 'claude-'")
        return value
    if spec.kind == "markets":
        return _parse_markets(raw)
    if spec.kind == "lines":
        return _parse_lines(raw)
    if spec.kind == "events":
        return _parse_events(raw)
    value = str(raw).strip()
    if not value:
        raise ValueError("mag niet leeg zijn")
    if len(value) > 2000:
        raise ValueError("te lang")
    return value


def parse_values(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate a (partial) set of settings. Raises SettingsError with all problems."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in raw.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            errors[key] = "onbekende of niet aanpasbare instelling"
            continue
        try:
            values[key] = parse_value(spec, value)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise SettingsError(errors)
    return values


def check_ceilings(values: dict[str, Any], env: Settings) -> dict[str, str]:
    """Values above the environment's hard ceiling; only the server environment can raise it."""
    errors: dict[str, str] = {}
    for key, value in values.items():
        ceiling = ceiling_of(env, key)
        if ceiling is not None and value > ceiling:
            spec = SPEC_BY_KEY[key]
            errors[key] = (
                f"maximaal {display_value(spec, ceiling)}: dat plafond staat in de serveromgeving "
                f"en is hier niet te verhogen ({CEILINGS[key].upper()})"
            )
    return errors


def check_consistency(settings: Settings) -> dict[str, str]:
    """Rules that span more than one field; checked on the merged result."""
    errors: dict[str, str] = {}
    if settings.min_trade_eur > settings.max_trade_eur:
        errors["min_trade_eur"] = "het minimum per order kan niet boven het maximum liggen"
    return errors


def preview(session: Session, values: dict[str, Any], env: Settings | None = None) -> Settings:
    """The settings as they would be after saving ``values`` (already parsed), fully validated.

    Raises SettingsError when the Settings model or a cross-field rule rejects the result.
    """
    env = env or get_settings()
    errors = check_ceilings(values, env)
    if errors:
        raise SettingsError(errors)
    merged = {**load_overrides(session), **values}
    try:
        candidate = _build(env, merged)
    except ValidationError as exc:
        errors = {}
        for e in exc.errors():
            key = str(e["loc"][0]) if e["loc"] else "?"
            errors[key] = e["msg"]
        raise SettingsError(errors) from exc
    errors = check_consistency(candidate)
    if errors:
        raise SettingsError(errors)
    return candidate


def unknown_markets(markets: list[str], known: set[str]) -> str | None:
    """Why some of ``markets`` cannot be traded: they are not in Bitvavo's market list
    (``known``). None when all exist. Suggests the closest existing market for a typo."""
    problems = []
    for market in markets:
        if market in known:
            continue
        close = difflib.get_close_matches(
            market, sorted(m for m in known if m.endswith("-EUR")), n=1, cutoff=0.75
        )
        hint = f" Bedoel je {close[0]}?" if close else ""
        problems.append(f"{market} bestaat niet op Bitvavo.{hint}")
    return " ".join(problems) or None


def model_warning(name: Any) -> str | None:
    """A warning (never a refusal) for a model name the app does not know: probably a typo,
    and the cost estimate then assumes the most expensive tier."""
    if not isinstance(name, str) or name in KNOWN_MODELS:
        return None
    return (
        "Onbekend model: controleer de naam. Een typfout merk je pas bij de volgende "
        "aanroep, en de kostenschatting rekent dan met het duurste tarief."
    )


# -- how values are shown --------------------------------------------------------------------


def number_text(value: float | int) -> str:
    """A number as the form field shows it, readable back by parse_decimal: a decimal comma and
    no thousands separator ('2,5', '1000', '0,25'). Never an exponent ('1e+07')."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    whole, _, frac = f"{value:.8f}".rstrip("0").partition(".")
    if len(frac) == 3 and whole.lstrip("-0"):
        frac += "0"  # '1234,5670': three decimals would read as a thousands separator
    return f"{whole},{frac}" if frac else whole


def _dutch(value: float) -> str:
    """'1.000', '2,5', '0,25': thousands with a point, up to four decimals."""
    if value == int(value):
        return num(value, 0)
    return num(value, len(f"{value:.4f}".rstrip("0").partition(".")[2]))


def display_value(spec: Spec, value: Any) -> str:
    """A value in a sentence, with its unit and in Dutch notation: '€ 1.000', '2,5%',
    '60 minuten', 'aan', 'Oefenmodus', 'BTC-EUR, ETH-EUR', '3 regels'."""
    if spec.kind == "bool":
        return "aan" if value else "uit"
    if spec.kind == "choice":
        return dict(spec.choices).get(value, str(value))
    if spec.kind == "markets":
        return ", ".join(value)
    if spec.kind == "lines":
        return f"{len(value)} feed{'' if len(value) == 1 else 's'}"
    if spec.kind == "events":
        return f"{len(value)} regel{'' if len(value) == 1 else 's'}"
    if spec.kind in ("int", "float") and isinstance(value, int | float):
        text = _dutch(float(value))
        if spec.unit in ("€", "$"):
            money = num(value, 0 if value == int(value) else 2)
            return f"{spec.unit} {money}"
        if spec.unit == "%":
            return f"{text}%"
        return f"{text} {spec.unit}" if spec.unit else text
    return str(value)


def form_text(spec: Spec, value: Any) -> str:
    """The value as the form submits it: what the page compares to see an unsaved edit."""
    if spec.kind == "bool":
        return "true" if value else "false"
    if spec.kind == "markets":
        return ", ".join(value)
    if spec.kind in ("lines", "events"):
        return "\n".join(value)
    if spec.kind in ("int", "float") and isinstance(value, int | float):
        return number_text(value)
    return str(value)


def as_text(spec: Spec, value: Any) -> str:
    """How a value is shown in a form field (and, for switches and choices, in words)."""
    if spec.kind == "bool":
        return "aan" if value else "uit"
    if spec.kind == "choice":
        return dict(spec.choices).get(value, str(value))
    return form_text(spec, value)


def describe_change(spec: Spec, old: Any, new: Any) -> str:
    """'Maximum per order: € 100 → € 25,50'; lists say what was added and removed."""
    if spec.kind in ("lines", "events") and isinstance(old, list) and isinstance(new, list):
        added = len([v for v in new if v not in old])
        removed = len([v for v in old if v not in new])
        parts = []
        if added:
            parts.append(f"{added} toegevoegd")
        if removed:
            parts.append(f"{removed} verwijderd")
        word = "regel" if spec.kind == "events" else "feed"
        what = ", ".join(parts) if parts else "volgorde aangepast"
        return f"{spec.label}: {what} (nu {len(new)} {word}{'' if len(new) == 1 else 's'})"
    return f"{spec.label}: {display_value(spec, old)} → {display_value(spec, new)}"


def changes(old: Settings, new: Settings, keys: list[str] | None = None) -> list[str]:
    """Every editable setting (or only ``keys``) whose value differs, as describe_change."""
    out = []
    for key in keys or EDITABLE_KEYS:
        spec = SPEC_BY_KEY[key]
        before, after = getattr(old, key), getattr(new, key)
        if before != after:
            out.append(describe_change(spec, before, after))
    return out


def loosened_limits(old: Settings, new: Settings) -> list[str]:
    """The hard limits that ``new`` makes more than twice as loose as ``old`` in one go, as
    'Maximum per order: € 100 → € 500'. The portal asks for an extra confirmation then."""
    out = []
    for key, rule in LOOSEN_RULES.items():
        before, after = float(getattr(old, key)), float(getattr(new, key))
        if before == after:
            continue
        if rule == "higher":
            loose = after > 2 * before if before > 0 else after > 0
        elif rule == "lower":
            loose = before > 0 and after < before / 2
        else:  # capital: more money for the bot; 0 means the whole account
            loose = after == 0 or (before > 0 and after > 2 * before)
        if loose:
            spec = SPEC_BY_KEY[key]
            text = describe_change(spec, getattr(old, key), getattr(new, key))
            if rule == "capital" and after == 0:
                text += " (het hele Bitvavo-account)"
            out.append(text)
    return out


# -- description for UI and API ----------------------------------------------------------


def _depends_text(spec: Spec) -> str:
    parent = SPEC_BY_KEY.get(spec.depends)
    if parent is None:
        return ""
    if parent.kind == "bool":
        return f"Geldt alleen als '{parent.label}' aan staat."
    return f"Geldt alleen als '{parent.label}' niet op 0 staat."


def _field(spec: Spec, current: Settings, env: Settings, overrides: dict[str, Any]) -> dict:
    ceiling = ceiling_of(env, spec.key)
    value = getattr(current, spec.key)
    default = getattr(env, spec.key)
    return {
        "key": spec.key,
        "label": spec.label,
        "help": spec.help,
        "kind": spec.kind,
        "choices": list(spec.choices),
        "min": spec.min,
        "max": spec.max if ceiling is None else min(spec.max or ceiling, ceiling),
        "ceiling": ceiling,
        "ceiling_display": display_value(spec, ceiling) if ceiling is not None else None,
        "ceiling_env": CEILINGS[spec.key].upper() if ceiling is not None else None,
        "unit": spec.unit,
        "env_name": spec.env_name,
        "value": value,
        "text": as_text(spec, value),
        "saved_text": form_text(spec, value),
        "display": display_value(spec, value),
        "default": default,
        "default_text": as_text(spec, default),
        "default_form": form_text(spec, default),
        "default_display": display_value(spec, default),
        "overridden": spec.key in overrides,
        "sensitive": spec.key in SENSITIVE_KEYS,
        "section": spec.section,
        "depends": spec.depends,
        "depends_text": _depends_text(spec),
        "placeholder": spec.placeholder,
        "loosen": LOOSEN_RULES.get(spec.key, ""),
        "warning": model_warning(value) if spec.kind == "model" else None,
    }


def describe_field(
    session: Session, current: Settings, key: str, env: Settings | None = None
) -> dict:
    """One field as describe() shows it (for settings outside the blocks: the start amount)."""
    env = env or get_settings()
    return _field(SPEC_BY_KEY[key], current, env, load_overrides(session))


def describe(session: Session, current: Settings, env: Settings | None = None) -> list[dict]:
    """Groups with their fields: current value, environment default and whether overridden.
    Each group also lists its parts (``sections``) with their fields, in page order."""
    env = env or get_settings()
    overrides = load_overrides(session)
    groups: list[dict] = []
    for group, (title, desc) in GROUPS.items():
        fields = [_field(SPEC_BY_KEY[k], current, env, overrides) for k in keys_in_form(group)]
        sections = []
        parts = [("", "", "")] if group in PART_GROUPS else SECTIONS.get(group, [("", "", "")])
        for key, sec_title, sec_help in parts:
            if key in _SPLIT_SECTIONS:
                continue
            in_section = [f for f in fields if f["section"] == key or not key]
            if in_section:
                sections.append(
                    {"key": key, "title": sec_title, "help": sec_help, "fields": in_section}
                )
        groups.append(
            {
                "key": group,
                "group": group,
                "part": "",
                "title": title,
                "description": desc,
                "fields": fields,
                "sections": sections,
                "sensitive": group_has_sensitive(group),
                "step_up_help": STEP_UP_HELP.get(group, ""),
                "summary": block_summary(group, fields),
            }
        )
    for part, (group, title, desc) in OWN_FORM_PARTS.items():
        specs = [s for s in SPECS if s.group == group and s.section == part]
        fields = [_field(s, current, env, overrides) for s in specs]
        groups.append(
            {
                "key": part,
                "group": group,
                "part": part,
                "title": title,
                "description": desc,
                "fields": fields,
                "sections": [{"key": "", "title": "", "help": "", "fields": fields}],
                "sensitive": any(s.key in SENSITIVE_KEYS for s in specs),
                "step_up_help": "",
                "summary": block_summary(part, fields),
            }
        )
    return groups
