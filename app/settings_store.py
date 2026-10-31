"""Settings that can be changed in the portal.

Values saved here live in the ``settings`` table (keys ``config:<name>``) and win over the
environment; the environment (Coolify / .env) is only the fallback and supplies the
defaults. Secrets (API keys, tokens, the database URL) and the LIVE_TRADING_ACK safeguard
are deliberately *not* editable here: they stay in the environment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.budget import PRICES
from app.config import Settings, get_settings
from app.db import Setting

log = logging.getLogger(__name__)

CONFIG_PREFIX = "config:"
MARKET_RE = re.compile(r"^[A-Z0-9]{2,10}-EUR$")

GROUPS: dict[str, tuple[str, str]] = {
    "trading": (
        "Handelen",
        "Wat de bot doet, hoe vaak en met welk geld.",
    ),
    "limits": (
        "Harde limieten",
        "Grenzen die altijd gelden, wat Claude ook adviseert. Ze worden in code afgedwongen.",
    ),
    "claude": (
        "Claude",
        "Welk model voor welke taak, en hoeveel het per maand mag kosten.",
    ),
    "news": (
        "Nieuws",
        "Welke bronnen worden gelezen en hoe vaak.",
    ),
}


@dataclass(frozen=True)
class Spec:
    """One editable setting: how to show it, parse it and check it."""

    key: str
    group: str
    label: str
    help: str = ""
    kind: str = "str"  # bool | int | float | str | choice | model | markets | lines
    choices: tuple[tuple[str, str], ...] = ()
    min: float | None = None
    max: float | None = None
    unit: str = ""
    restart: bool = False  # True when a change only takes effect after a restart
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def env_name(self) -> str:
        return self.key.upper()


MODEL_CHOICES = tuple((m, m) for m in PRICES)

SPECS: tuple[Spec, ...] = (
    # -- trading --
    Spec(
        "trading_mode",
        "trading",
        "Modus",
        "Oefenmodus handelt met fictief geld. Live plaatst echte orders op Bitvavo en kan "
        "alleen aan als op de server LIVE_TRADING_ACK=true staat en er een Bitvavo-sleutel "
        "met handelsrechten is.",
        kind="choice",
        choices=(("paper", "Oefenmodus"), ("live", "Live")),
    ),
    Spec(
        "trading_enabled",
        "trading",
        "Bot automatisch laten handelen",
        "Uit: de bot draait geen cycli meer, behalve als je er zelf een start.",
        kind="bool",
    ),
    Spec(
        "markets",
        "trading",
        "Markten",
        "Bitvavo-notatie, gescheiden door komma's. Alleen euromarkten, bijvoorbeeld BTC-EUR, "
        "ETH-EUR, SOL-EUR.",
        kind="markets",
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
    ),
    Spec(
        "watch_interval_min",
        "trading",
        "Dipbewaking: controle elke",
        "Zodra de dip-voorwaarden van de strategie gelden (bijvoorbeeld RSI onder 35) maar de "
        "draai nog niet, kijkt de bot zo vaak opnieuw naar die markt. Draait de indicator "
        "weer omhoog, dan gaat de situatie meteen naar Claude. 0 zet de bewaking uit.",
        kind="int",
        min=0,
        max=60,
        unit="minuten",
    ),
    Spec(
        "watch_max_min",
        "trading",
        "Dipbewaking: maximaal",
        "Hoe lang een dip bewaakt wordt; daarna wacht de bot weer op de gewone cyclus.",
        kind="int",
        min=5,
        max=1440,
        unit="minuten",
    ),
    Spec(
        "trend_context_enabled",
        "trading",
        "Lange-termijntrend (PlanB)",
        "Per coin het 200-weeks gemiddelde en de maand-RSI uit Bitvavo, dagelijks ververst. "
        "Claude krijgt het als context (opwaartse of neerwaartse lange trend); het is geen "
        "signaal.",
        kind="bool",
    ),
    Spec(
        "decision_history_days",
        "trading",
        "Beslissingen tonen van de laatste",
        "Zo veel dagen aan cycli staan op de pagina Handelen, per markt met signaal, "
        "indicatorwaarden, nieuws en toelichting.",
        kind="int",
        min=1,
        max=365,
        unit="dagen",
    ),
    Spec(
        "paper_start_eur",
        "trading",
        "Startbedrag oefenmodus",
        "Fictief saldo waarmee de oefenportefeuille begint. Geldt bij een nieuwe of "
        "gereset oefenportefeuille.",
        kind="float",
        min=10,
        max=10_000_000,
        unit="€",
    ),
    Spec(
        "claude_unavailable_mode",
        "trading",
        "Als Claude niet beschikbaar is",
        "Bij een signaal zonder werkende Claude (budget op, geen sleutel, storing).",
        kind="choice",
        choices=(("hold", "Afwachten, niets doen"), ("indicators", "Het strategiesignaal volgen")),
    ),
    # -- limits --
    Spec(
        "max_trade_eur",
        "limits",
        "Maximum per order",
        "Claude mag nooit meer dan dit bedrag in één order uitgeven.",
        kind="float",
        min=5,
        max=1_000_000,
        unit="€",
    ),
    Spec(
        "min_trade_eur",
        "limits",
        "Minimum per order",
        "Kleinere orders worden niet geplaatst. Bitvavo accepteert minimaal € 5.",
        kind="float",
        min=5,
        max=100_000,
        unit="€",
    ),
    Spec(
        "max_exposure_pct",
        "limits",
        "Maximaal in crypto",
        "Percentage van de totale portefeuille dat in coins mag zitten.",
        kind="float",
        min=0,
        max=100,
        unit="%",
    ),
    Spec(
        "daily_loss_limit_pct",
        "limits",
        "Dagverlieslimiet",
        "Bij dit gerealiseerde verlies op één dag koopt de bot de rest van de dag niets.",
        kind="float",
        min=0.1,
        max=100,
        unit="%",
    ),
    Spec(
        "max_trades_per_day",
        "limits",
        "Maximum aantal trades per dag",
        "Uitgevoerde koop- en verkooporders van de bot, handmatige orders tellen niet mee.",
        kind="int",
        min=0,
        max=200,
    ),
    Spec(
        "cooldown_min_after_stop",
        "limits",
        "Afkoelperiode na stop-loss",
        "Zo lang koopt de bot niet opnieuw in een markt waar een stop-loss is geraakt.",
        kind="int",
        min=0,
        max=10_080,
        unit="minuten",
    ),
    # -- claude --
    Spec(
        "claude_monthly_budget_usd",
        "claude",
        "Maandbudget",
        "Harde grens voor de Claude-kosten. Bij 90% waarschuwt het overzicht, bij 100% "
        "stoppen alle Claude-aanroepen tot de volgende maand.",
        kind="float",
        min=0,
        max=100_000,
        unit="$",
    ),
    Spec(
        "claude_model_decision",
        "claude",
        "Model voor handelsbeslissingen",
        "Wordt elke cyclus met een signaal aangeroepen; Sonnet is hier de balans tussen "
        "kwaliteit en kosten.",
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
        "Alleen als jij op de strategiepagina een voorstel vraagt.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "claude_model_evaluation",
        "claude",
        "Model voor de evaluatie",
        "Zeldzaam en diepgaand, dus het sterkste model is hier betaalbaar.",
        kind="model",
        choices=MODEL_CHOICES,
    ),
    Spec(
        "evaluation_interval_hours",
        "claude",
        "Evaluatie-interval",
        "Hoe vaak Claude terugkijkt op de resultaten en eventueel een strategie voorstelt. "
        "0 schakelt de automatische evaluatie uit.",
        kind="int",
        min=0,
        max=8760,
        unit="uur",
    ),
    Spec(
        "trading_fee_pct",
        "limits",
        "Handelskosten per order",
        "Bitvavo taker-fee per kant die oefenorders betalen en waarmee resultaten netto worden "
        "berekend. Met een live key leest de app je echte tarief bij het opstarten.",
        kind="float",
        min=0,
        max=5,
        unit="%",
    ),
    Spec(
        "backtest_fee_pct",
        "claude",
        "Handelskosten in backtests",
        "Bitvavo-fee per order die backtests meerekenen.",
        kind="float",
        min=0,
        max=5,
        unit="%",
    ),
    # -- news --
    Spec(
        "news_enabled",
        "news",
        "Nieuws automatisch ophalen",
        "Uit: nieuws wordt alleen opgehaald als je op de nieuwspagina op 'Nu verversen' klikt.",
        kind="bool",
    ),
    Spec(
        "news_fetch_interval_min",
        "news",
        "Interval nieuws ophalen",
        "",
        kind="int",
        min=1,
        max=1440,
        unit="minuten",
    ),
    Spec(
        "news_rss_feeds",
        "news",
        "RSS-feeds",
        "Eén adres per regel. Standaard: CoinDesk, Cointelegraph, The Block, Decrypt, "
        "CryptoSlate, NewsBTC, BeInCrypto en DL News.",
        kind="lines",
    ),
    Spec(
        "cryptocv_enabled",
        "news",
        "cryptocurrency.cv",
        "Gratis verzamelaar van 300+ bronnen zonder sleutel, met een archief per coin. "
        "Berichten komen direct met de juiste coin-tag binnen.",
        kind="bool",
    ),
    Spec(
        "google_news_enabled",
        "news",
        "Google News per coin",
        "Gratis zoekfeed per markt (bijvoorbeeld 'solana') van de laatste 24 uur.",
        kind="bool",
    ),
    Spec(
        "fear_greed_enabled",
        "news",
        "Fear & Greed-index",
        "Marktbrede angst/hebzucht-score van alternative.me; Claude krijgt hem als context.",
        kind="bool",
    ),
    Spec(
        "gdelt_enabled",
        "news",
        "Wereldnieuws via GDELT",
        "Gratis, maar maximaal één verzoek per 5 seconden per IP-adres.",
        kind="bool",
    ),
    Spec(
        "gdelt_query",
        "news",
        "GDELT-zoekopdracht",
        "Zoektermen in de GDELT-syntaxis.",
        kind="str",
    ),
    Spec(
        "news_max_age_hours",
        "news",
        "Berichten ouder dan dit negeren",
        "",
        kind="int",
        min=1,
        max=720,
        unit="uur",
    ),
    Spec(
        "news_batch_size",
        "news",
        "Berichten per Claude-aanroep",
        "Grotere batches zijn goedkoper, maar te groot maakt de beoordeling slordiger.",
        kind="int",
        min=1,
        max=50,
    ),
    Spec(
        "cryptopanic_plan",
        "news",
        "CryptoPanic-abonnement",
        "Alleen van belang als er een CRYPTOPANIC_TOKEN in de omgeving staat.",
        kind="choice",
        choices=(
            ("growth", "Growth"),
            ("growth_weekly", "Growth (weekly)"),
            ("enterprise", "Enterprise"),
        ),
    ),
)

SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}
EDITABLE_KEYS = tuple(SPEC_BY_KEY)


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
    overrides = load_overrides(session)
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
    return Settings(**overrides)


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


def _parse_number(spec: Spec, raw: Any) -> float | int:
    if isinstance(raw, bool):
        raise ValueError("getal verwacht")
    text = str(raw).strip().replace(",", ".")
    if text == "":
        raise ValueError("vul een getal in")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError("geen geldig getal") from exc
    if spec.kind == "int":
        if value != int(value):
            raise ValueError("heel getal verwacht")
        value = int(value)
    if spec.min is not None and value < spec.min:
        raise ValueError(f"minimaal {spec.min:g}{' ' + spec.unit if spec.unit else ''}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"maximaal {spec.max:g}{' ' + spec.unit if spec.unit else ''}")
    return value


def _parse_markets(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    markets: list[str] = []
    for part in parts:
        m = str(part).strip().upper()
        if not m:
            continue
        if not MARKET_RE.match(m):
            raise ValueError(f"{m} is geen euromarkt in Bitvavo-notatie (bijvoorbeeld BTC-EUR)")
        if m not in markets:
            markets.append(m)
    if not markets:
        raise ValueError("kies minstens één markt")
    return markets


def _parse_lines(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, list) else re.split(r"[\r\n,]+", str(raw))
    urls: list[str] = []
    for part in parts:
        url = str(part).strip()
        if not url:
            continue
        if not re.match(r"^https?://\S+$", url):
            raise ValueError(f"'{url[:60]}' is geen geldig adres (begin met http:// of https://)")
        if url not in urls:
            urls.append(url)
    if not urls:
        raise ValueError("vul minstens één feed in")
    return urls


def parse_value(spec: Spec, raw: Any) -> Any:
    """Turn a form/JSON value into the typed value the Settings model expects."""
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


def as_text(spec: Spec, value: Any) -> str:
    """How a value is shown in a form field."""
    if spec.kind == "markets":
        return ", ".join(value)
    if spec.kind == "lines":
        return "\n".join(value)
    if spec.kind == "bool":
        return "aan" if value else "uit"
    if spec.kind == "choice":
        return dict(spec.choices).get(value, str(value))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# -- description for UI and API ----------------------------------------------------------


def describe(session: Session, current: Settings, env: Settings | None = None) -> list[dict]:
    """Groups with their fields: current value, environment default and whether overridden."""
    env = env or get_settings()
    overrides = load_overrides(session)
    groups: list[dict] = []
    for group, (title, desc) in GROUPS.items():
        fields = []
        for spec in SPECS:
            if spec.group != group:
                continue
            fields.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "help": spec.help,
                    "kind": spec.kind,
                    "choices": list(spec.choices),
                    "min": spec.min,
                    "max": spec.max,
                    "unit": spec.unit,
                    "env_name": spec.env_name,
                    "value": getattr(current, spec.key),
                    "text": as_text(spec, getattr(current, spec.key)),
                    "default": getattr(env, spec.key),
                    "default_text": as_text(spec, getattr(env, spec.key)),
                    "overridden": spec.key in overrides,
                }
            )
        groups.append({"key": group, "title": title, "description": desc, "fields": fields})
    return groups
