"""Jinja templates, static files and the Dutch formatting filters used by the pages."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.strategy.describe import (  # noqa: F401
    OPERATORS_NL,
    SERIES_NL,
    condition_nl,
    format_value,
)
from app.strategy.model import IndicatorSpec

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Bumped whenever app.css changes so browsers pick up the new file.
ASSET_VERSION = "11"

# Internal identifiers -> what the user sees.
LABELS_NL: dict[str, str] = {
    "paper": "Oefenmodus",
    "live": "Live",
    "buy": "Kopen",
    "sell": "Verkopen",
    "hold": "Afwachten",
    "claude": "Claude",
    "rules": "Regels",
    "mechanical": "Automatisch",
    "manual": "Handmatig",
    "filled": "Uitgevoerd",
    "new": "Geplaatst",
    "canceled": "Geannuleerd",
    "rejected": "Afgewezen",
    "unknown": "Onbekend",
    "open": "Open",
    "closed": "Gesloten",
    "stop_loss": "Stop-loss",
    "trailing_stop": "Trailing stop",
    "take_profit": "Take-profit",
    "signal": "Verkoopsignaal",
    "news_exit": "Nieuws",
    "reset": "Reset oefenportefeuille",
    "win": "Winst",
    "loss": "Verlies",
    "flat": "Break-even",
    "error": "Fout",
    "watch": "Dipbewaking",
    "hours": "uren",
    "days": "dagen",
    "weeks": "weken",
    "rss": "RSS",
    "gdelt": "GDELT",
    "cryptopanic": "CryptoPanic",
    "cryptocv": "cryptocurrency.cv",
    "googlenews": "Google News",
    "1m": "1 minuut",
    "5m": "5 minuten",
    "15m": "15 minuten",
    "30m": "30 minuten",
    "1h": "1 uur",
    "2h": "2 uur",
    "4h": "4 uur",
    "6h": "6 uur",
    "8h": "8 uur",
    "12h": "12 uur",
    "1d": "1 dag",
}

MONTHS_NL = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def nl(value: object) -> str:
    """Translate an internal identifier; unknown values are shown as they are."""
    if value is None:
        return ""
    return LABELS_NL.get(str(value), str(value))


def _dutch_number(value: float, decimals: int) -> str:
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")


def num(value: object, decimals: int = 2) -> str:
    if value is None:
        return "–"
    return _dutch_number(float(value), decimals)


def eur(value: object, decimals: int = 2) -> str:
    if value is None:
        return "–"
    v = float(value)
    sign = "-" if v < 0 else ""
    return f"{sign}€ {_dutch_number(abs(v), decimals)}"


def pct(value: object, decimals: int = 1, signed: bool = True) -> str:
    if value is None:
        return "–"
    v = float(value)
    prefix = "+" if signed and v > 0 else ""
    return f"{prefix}{_dutch_number(v, decimals)}%"


def price(value: object) -> str:
    """Coin prices: two decimals for big numbers, more for cheap coins."""
    if value is None:
        return "–"
    v = float(value)
    decimals = 2 if abs(v) >= 10 else 4 if abs(v) >= 0.1 else 6
    return _dutch_number(v, decimals)


def amount(value: object) -> str:
    """Coin amounts: trim trailing zeros but keep enough precision."""
    if value is None:
        return "–"
    text = f"{float(value):.8f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    whole = _dutch_number(float(whole), 0)
    return f"{whole},{frac}" if frac else whole


def dt(value: datetime | None, with_year: bool = False) -> str:
    """'31 okt 14:05' (UTC), or '–' when there is no timestamp."""
    if value is None:
        return "–"
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    year = f" {value.year}" if with_year else ""
    return f"{value.day} {MONTHS_NL[value.month - 1]}{year} {value:%H:%M}"


def isodt(value: str | datetime | None) -> str:
    """ISO string or datetime -> '31 okt 14:05' (UTC)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return dt(value)


def ts(value: int | None) -> str:
    """Epoch seconds -> '31 okt 14:05'."""
    if value is None:
        return "–"
    return dt(datetime.fromtimestamp(int(value), UTC))


def ago(value: datetime | None, now: datetime | None = None) -> str:
    """Relative time in Dutch: 'zojuist', '5 min geleden', '3 uur geleden', '2 dagen geleden'."""
    if value is None:
        return "nog niet"
    now = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return "zojuist"
    if seconds < 3600:
        return f"{seconds // 60} min geleden"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} uur geleden"
    days = seconds // 86400
    return "gisteren" if days == 1 else f"{days} dagen geleden"


def indicator_nl(ind: IndicatorSpec) -> str:
    if ind.type == "macd":
        return f"MACD {ind.fast}/{ind.slow}/{ind.signal}"
    if ind.type == "bollinger":
        return f"Bollinger {ind.period} (σ {ind.std_dev:g})"
    if ind.type == "drawdown":
        return f"% onder hoogste koers van {ind.period} candles"
    return f"{ind.type.upper()} {ind.period}"


def sentiment_class(score: float | None, threshold: float = 0.1) -> str:
    if score is None:
        return ""
    return "pos" if score > threshold else "neg" if score < -threshold else ""


def sentiment_pill(label: str) -> str:
    if "positief" in label:
        return "pill-green"
    if "negatief" in label:
        return "pill-red"
    if label == "geen nieuws":
        return ""
    return "pill-blue"


def action_pill(action: str | None) -> str:
    return {"buy": "pill-green", "sell": "pill-red", "hold": ""}.get(action or "", "")


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.filters.update(
    {
        "nl": nl,
        "num": num,
        "eur": eur,
        "pct": pct,
        "price": price,
        "amount": amount,
        "dt": dt,
        "isodt": isodt,
        "ts": ts,
        "ago": ago,
        "indicator_nl": indicator_nl,
        "condition_nl": condition_nl,
        "compact": format_value,
        "sentiment_class": sentiment_class,
        "sentiment_pill": sentiment_pill,
        "action_pill": action_pill,
        # Backwards compatibility with the old templates' epoch filter.
        "datetimeformat": ts,
    }
)
