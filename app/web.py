"""Jinja templates, static files and the Dutch formatting filters used by the pages."""

from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.flash import take_flashes
from app.labels import CLOSE_REASONS_NL as _CLOSE_REASONS_NL
from app.labels import (
    KILL_SWITCH_HELP_NL,
    daily_loss_rule_nl,
    limit_pct,
    max_orders_rule_nl,
    orders_limit_nl,
)
from app.strategy.describe import (  # noqa: F401
    OPERATORS_NL,
    SERIES_NL,
    condition_nl,
    format_value,
    significant_decimals,
)
from app.strategy.model import IndicatorSpec
from app.strategy.page import description_outdated

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Bumped whenever app.css or a script in /static changes so browsers pick up the new file.
ASSET_VERSION = "16"

NBSP = " "  # between the euro sign and the amount, so '€ 1.000,00' never breaks

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
    "partiallyFilled": "Deels uitgevoerd",
    "new": "Geplaatst",
    "pending": "Bezig",
    "canceled": "Geannuleerd",
    "cancelled": "Geannuleerd",
    "expired": "Verlopen",
    "rejected": "Afgewezen",
    "unknown": "Onbekend",
    "open": "Open",
    "opening": "Wordt geopend",
    "closing": "Wordt gesloten",
    "closed": "Gesloten",
    "stop_loss": "Stop-loss",
    "trailing_stop": "Trailing stop",
    "take_profit": "Take-profit",
    "signal": "Verkoopsignaal",
    "news_exit": "Negatief nieuws",
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

# Position.close_reason -> how a closed trade ended, as a phrase ('Reden: trailing stop geraakt').
# One list for every page (app/labels.py, shared with the journal); the short labels for table
# cells stay in LABELS_NL.
CLOSE_REASONS_NL = _CLOSE_REASONS_NL

# Who wrote an order reason: the engine prefixes it with the source ('claude: …').
ORDER_REASON_SOURCES = ("claude", "rules", "manual", "mechanical")

MODES_NL: dict[str, dict[str, str]] = {
    "paper": {
        "label": "Oefenmodus",
        "money": "fictief geld",
        "explain": "Oefenmodus: de bot handelt met fictief geld, er gaan geen orders naar Bitvavo.",
    },
    "live": {
        "label": "Live",
        "money": "echt geld",
        "explain": "Live: de bot plaatst echte orders met echt geld op Bitvavo.",
    },
}
LIVE_BANNER = "LIVE · echt geld op Bitvavo"

# The kill switch: one name and one explanation on every page (see TradingEngine: a pause
# blocks every new buy, bot and manual; the exit loop and sells on a sell signal keep running).
KILL_SWITCH_NL: dict[str, str] = {
    "name": "Noodstop",
    "button": "Noodstop: kopen pauzeren",
    "state": "Noodstop actief: kopen gepauzeerd",
    "resume": "Kopen hervatten",
    "help": KILL_SWITCH_HELP_NL,
    "detail": "De cycli lopen door, dus Claude kan nog worden gevraagd (dat kost budget).",
    "confirm_pause": (
        "Noodstop: kopen pauzeren? Tot je hervat koopt niemand iets, de bot niet en jij niet. "
        "Stop-loss, trailing stop, take-profit en verkopen op een verkoopsignaal blijven "
        "werken; een verkoop alleen op negatief nieuws wacht. De cycli lopen door, dus Claude "
        "kan nog worden gevraagd (dat kost budget)."
    ),
    "confirm_resume": "Kopen hervatten? De bot mag dan weer kopen.",
}


class Html(str):
    """Text that is already safe HTML: Jinja leaves objects with ``__html__`` unescaped, and
    joins them with escaped plain text (``"Laatste: " ~ x|dt``)."""

    __slots__ = ()

    def __html__(self) -> str:
        return self


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


MONTHS_NL = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def nl(value: object) -> str:
    """Translate an internal identifier; unknown values are shown as they are."""
    if value is None:
        return ""
    return LABELS_NL.get(str(value), str(value))


def close_reason(value: object) -> str:
    """'trailing_stop' -> 'trailing stop geraakt'; unknown reasons as they are."""
    if value is None:
        return ""
    return CLOSE_REASONS_NL.get(str(value), nl(value))


def order_reason(value: object) -> dict[str, str]:
    """Split an order reason into its source and the text: 'claude: dip gekocht' ->
    {'source': 'claude', 'source_nl': 'Claude', 'text': 'dip gekocht'}. Exit orders carry a
    bare close reason ('stop_loss'), which becomes its phrase."""
    text = str(value or "").strip()
    head, sep, rest = text.partition(":")
    if sep and head in ORDER_REASON_SOURCES:
        return {"source": head, "source_nl": nl(head), "text": rest.strip()}
    if text in ORDER_REASON_SOURCES:
        return {"source": text, "source_nl": nl(text), "text": ""}
    if text in CLOSE_REASONS_NL:
        return {"source": "mechanical", "source_nl": nl("mechanical"), "text": close_reason(text)}
    return {"source": "", "source_nl": "", "text": text}


def _finite(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _dutch_number(value: float, decimals: int) -> str:
    v = round(value, decimals)
    if v == 0:
        v = 0.0  # never '-0,00'
    text = f"{v:,.{decimals}f}"
    return text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")


def num(value: object, decimals: int = 2) -> str:
    v = _finite(value)
    if v is None:
        return "–"
    return _dutch_number(v, decimals)


def eur(value: object, decimals: int = 2, signed: bool = False) -> str:
    """'€ 1.234,50', '-€ 3,20' (sign before the euro sign), '€ 0,00' for a rounded zero.
    ``signed`` adds '+' to a gain. The space after € is a no-break space."""
    v = _finite(value)
    if v is None:
        return "–"
    v = round(v, decimals)
    if v == 0:
        v = 0.0
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}€{NBSP}{_dutch_number(abs(v), decimals)}"


def eur_short(value: object) -> str:
    """A limit or threshold: '€ 100' for a whole amount, '€ 7,50' otherwise (never '€ 8')."""
    v = _finite(value)
    if v is None:
        return "–"
    return eur(v, 0 if round(v, 2) == round(v) else 2)


def pct(value: object, decimals: int = 1, signed: bool = True) -> str:
    """'+2,3%', '-0,5%', and '0,0%' (never '-0,0%') for a change that rounds to zero."""
    v = _finite(value)
    if v is None:
        return "–"
    v = round(v, decimals)
    if v == 0:
        v = 0.0
    prefix = "+" if signed and v > 0 else ""
    return f"{prefix}{_dutch_number(v, decimals)}%"


def pct_limit(value: object) -> str:
    """A percentage setting or limit with the decimals it has: '5%', '2,5%' (never rounded to
    '3%'); '–' without a value."""
    v = _finite(value)
    if v is None:
        return "–"
    return limit_pct(v)


def pnl_class(value: object, decimals: int = 2) -> str:
    """CSS class for a result, decided on the value as it is shown: 'pos' (gain), 'neg'
    (loss), 'zero' (rounds to zero) or '' (no value). Use the decimals of the display
    filter: ``x|pnl_class`` next to ``x|eur``, ``x|pnl_class(1)`` next to ``x|pct``."""
    v = _finite(value)
    if v is None:
        return ""
    v = round(v, decimals)
    return "pos" if v > 0 else "neg" if v < 0 else "zero"


def price(value: object) -> str:
    """Coin prices with at least 4 significant digits: '45.000,12', '0,5234', '0,000004150'."""
    v = _finite(value)
    if v is None:
        return "–"
    return _dutch_number(v, significant_decimals(v, digits=4, minimum=2))


def price_eur(value: object) -> str:
    """A coin price with the euro sign: '€ 0,000004150' (no-break space)."""
    v = _finite(value)
    if v is None:
        return "–"
    return f"€{NBSP}{price(v)}"


def amount(value: object) -> str:
    """Coin amounts: trim trailing zeros but keep enough precision."""
    v = _finite(value)
    if v is None:
        return "–"
    text = f"{abs(v):.8f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    sign = "-" if v < 0 and text != "0" else ""
    whole = _dutch_number(float(whole), 0)
    return f"{sign}{whole},{frac}" if frac else f"{sign}{whole}"


# -- Time --------------------------------------------------------------------------------------
# One line for the whole app: the server writes every moment in UTC and says so ('2 nov 10:49
# UTC') inside <time datetime="…Z" data-local>. app.js rewrites those elements in the browser's
# own time zone with its abbreviation ('2 nov 11:49 CET'); without JavaScript the UTC text stays.


def _as_utc(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, int | float) and not isinstance(value, bool):
        value = datetime.fromtimestamp(int(value), UTC)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def dt(value: object, with_year: bool = False) -> str:
    """'31 okt 14:05 UTC' as plain text, or '–' when there is no timestamp. For attributes and
    sentences; in page markup the ``dt`` filter (``time_html``) is the one to use."""
    moment = _as_utc(value)
    if moment is None:
        return "–"
    year = f" {moment.year}" if with_year else ""
    return f"{moment.day} {MONTHS_NL[moment.month - 1]}{year} {moment:%H:%M} UTC"


def isodt(value: str | datetime | None) -> str:
    """ISO string or datetime -> '31 okt 14:05 UTC'."""
    return dt(value)


def ts(value: int | None) -> str:
    """Epoch seconds -> '31 okt 14:05 UTC'."""
    return dt(value)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def time_html(value: object, with_year: bool = False) -> Html:
    """<time datetime="2026-10-31T14:05:00Z" data-local="">31 okt 14:05 UTC</time>; app.js shows
    it in the browser's time zone. ``with_year`` keeps the year ('data-local="year"')."""
    moment = _as_utc(value)
    if moment is None:
        return Html("–")
    mode = "year" if with_year else ""
    return Html(
        f'<time datetime="{_iso(moment)}" data-local="{mode}">{_esc(dt(moment, with_year))}</time>'
    )


def ago(value: datetime | None, now: datetime | None = None) -> str:
    """Relative time in Dutch: 'zojuist', '5 min geleden', '3 uur geleden', '2 dagen geleden';
    a moment ahead reads 'over 5 min', 'over 3 uur', 'morgen'."""
    moment = _as_utc(value)
    if moment is None:
        return "nog niet"
    now = now or datetime.now(UTC)
    seconds = int((now - moment).total_seconds())
    if seconds <= -60:
        ahead = -seconds
        if ahead < 3600:
            return f"over {ahead // 60} min"
        if ahead < 86400:
            return f"over {ahead // 3600} uur"
        days = ahead // 86400
        return "morgen" if days == 1 else f"over {days} dagen"
    seconds = max(0, seconds)
    if seconds < 60:
        return "zojuist"
    if seconds < 3600:
        return f"{seconds // 60} min geleden"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} uur geleden"
    days = seconds // 86400
    return "gisteren" if days == 1 else f"{days} dagen geleden"


def ago_html(value: datetime | None, now: datetime | None = None) -> Html:
    """<time datetime="…Z" data-ts title="31 okt 14:05 UTC">5 min geleden</time>: app.js keeps
    the relative text up to date while the page stays open."""
    moment = _as_utc(value)
    if moment is None:
        return Html(_esc(ago(None)))
    return Html(
        f'<time datetime="{_iso(moment)}" data-ts title="{_esc(dt(moment))}">'
        f"{_esc(ago(moment, now))}</time>"
    )


# -- Markdown (untrusted text from Claude) -----------------------------------------------------
# Escape first, then turn a small safe subset into HTML: headings, paragraphs, lists, bold and
# italic. Links, images, code and raw HTML stay plain text, because the reports partly rest on
# untrusted news text.

_MD_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_MD_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_MD_BULLET = re.compile(r"^[ \t]*[-*+•][ \t]+(.*)$")
_MD_NUMBER = re.compile(r"^[ \t]*(\d{1,4})[.)][ \t]+(.*)$")
_MD_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
_MD_QUOTE = re.compile(r"^[ \t]*(?:>[ \t]?)+")
_MD_URL = r"\((?:[^()\s]|\([^()\s]*\))*\)"
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]" + _MD_URL)
_MD_LINK = re.compile(r"\[([^\]]+)\]" + _MD_URL)
_MD_STRONG = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
)
_MD_EM = (
    re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])"),
    re.compile(r"(?<![_\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![_\w])"),
)


def _md_inline(text: str) -> str:
    s = _esc(text)  # from here on every <, >, & and quote is an entity
    s = _MD_IMAGE.sub(r"\1", s)
    s = _MD_LINK.sub(r"\1", s)
    s = s.replace("`", "")
    for pattern in _MD_STRONG:
        s = pattern.sub(r"<strong>\1</strong>", s)
    for pattern in _MD_EM:
        s = pattern.sub(r"<em>\1</em>", s)
    return s


def md(text: object, heading: int = 3) -> Html:
    """Safe markdown subset -> HTML in <div class="md">. The highest markdown heading in the
    text becomes <h{heading}>, deeper ones follow (capped at h6)."""
    if text is None or str(text).strip() == "":
        return Html("")
    heading = min(6, max(2, int(heading)))
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    levels = [len(m.group(1)) for line in lines if (m := _MD_HEADING.match(line.strip()))]
    top = min(levels) if levels else 1
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""
    list_start = 1

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            start = f' start="{list_start}"' if list_tag == "ol" and list_start != 1 else ""
            body = "".join(f"<li>{item}</li>" for item in items)
            out.append(f"<{list_tag}{start}>{body}</{list_tag}>")
            items.clear()
        list_tag = ""

    for raw in lines:
        if _MD_FENCE.match(raw):
            continue
        line = _MD_QUOTE.sub("", raw)
        stripped = line.strip()
        if not stripped:
            flush_para()
            flush_list()
            continue
        if m := _MD_HEADING.match(stripped):
            flush_para()
            flush_list()
            level = min(6, heading + len(m.group(1)) - top)
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            continue
        if _MD_RULE.match(line):
            flush_para()
            flush_list()
            out.append("<hr>")
            continue
        bullet = _MD_BULLET.match(line)
        number = None if bullet else _MD_NUMBER.match(line)
        if bullet or number:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                flush_list()
                list_tag = tag
                list_start = int(number.group(1)) if number else 1
            items.append(_md_inline((bullet or number).group(1 if bullet else 2).strip()))
            continue
        if items and raw[:1] in (" ", "\t"):
            items[-1] += " " + _md_inline(stripped)  # continuation of a list item
            continue
        flush_list()
        para.append(_md_inline(stripped))
    flush_para()
    flush_list()
    return Html('<div class="md">' + "".join(out) + "</div>")


# A web address in the owner's own text (journal notes): http(s) only, closing punctuation
# stays outside the link. Runs on escaped text, so the address is already attribute-safe.
# An address in plain text: it ends at white space, a quote or an angle bracket. Matched on the
# raw text (not on escaped text, where '&quot;' or '&gt;' would become part of the link).
_URL = re.compile(r"\bhttps?://[^\s<>\"'`]+", re.IGNORECASE)
_URL_TAIL = ".,;:!?)]}"


def linkify(text: object) -> Html:
    """Escape ``text`` and make its http(s) addresses clickable (new tab, no referrer). Line
    breaks stay as they are (use it inside .prose). Every piece is escaped, the link too."""
    if text is None:
        return Html("")
    raw = str(text)
    out: list[str] = []
    pos = 0
    for match in _URL.finditer(raw):
        url, tail = match.group(0), ""
        while url and url[-1] in _URL_TAIL:
            tail = url[-1] + tail
            url = url[:-1]
        out.append(_esc(raw[pos : match.start()]))
        if re.fullmatch(r"https?://", url, re.IGNORECASE):
            out.append(_esc(url + tail))  # no host: nothing to link
        else:
            href = _esc(url)
            out.append(
                f'<a href="{href}" target="_blank" rel="noopener noreferrer">{href}</a>'
                + _esc(tail)
            )
        pos = match.end()
    out.append(_esc(raw[pos:]))
    return Html("".join(out))


def indicator_nl(ind: IndicatorSpec) -> str:
    if ind.type == "macd":
        return f"MACD {ind.fast}/{ind.slow}/{ind.signal}"
    if ind.type == "bollinger":
        return f"Bollinger {ind.period} (σ {ind.std_dev:g})"
    if ind.type == "drawdown":
        return f"% onder hoogste koers van {ind.period} candles"
    if ind.type == "fibonacci":
        return f"Fibonacci-retracement van de swing in {ind.period} candles"
    if ind.type == "ema_support":
        return f"EMA {ind.period} als steun (±{ind.tolerance_pct:g}%)"
    return f"{ind.type.upper()} {ind.period}"


def sentiment_class(score: float | None, threshold: float = 0.1) -> str:
    if score is None:
        return ""
    return "pos" if score > threshold else "neg" if score < -threshold else ""


def market_list(markets: object, show: int = 3) -> str:
    """'BTC-EUR, ETH-EUR', or with many markets 'BTC-EUR, ETH-EUR, SOL-EUR en 17 andere'
    (20 are allowed; a full list would fill the line)."""
    items = [str(m) for m in (markets or [])]
    if not items:
        return "geen markten"
    if len(items) > show + 1:
        return f"{', '.join(items[:show])} en {len(items) - show} andere"
    return ", ".join(items)


def hours_nl(hours: object) -> str:
    """An interval in hours as people say it: '7 dagen', '1 dag', '12 uur'."""
    try:
        h = int(hours)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "–"
    if h > 0 and h % 24 == 0:
        days = h // 24
        return f"{days} {'dag' if days == 1 else 'dagen'}"
    return f"{h} uur"


def capfirst(text: object) -> str:
    """First letter upper case, the rest untouched (Jinja's capitalize would write 'utc')."""
    out = str(text or "")
    return out[:1].upper() + out[1:]


def refusal_nl(text: object) -> str:
    """A refusal from the engine or the hard limits as the pages show it: the app's own words
    ('noodstop', 'orders per dag') and Dutch notation ('€ 100,00', '50%'). The engine keeps its
    wording for the API, the log and the stored decisions (tests pin it); every page passes it
    through here, so the order card, Handelen and the chart say the same thing."""
    out = str(text or "")
    out = out.replace(" (maximum per order)", "")
    out = out.replace(
        "handelen is gepauzeerd (kill switch); kopen kan niet",
        "de noodstop staat aan: kopen is gepauzeerd (verkopen kan wel)",
    )
    out = out.replace(
        "handelen is gepauzeerd (kill switch)", "de noodstop staat aan: kopen is gepauzeerd"
    )
    out = re.sub(r"\b(\d+) trades per dag", r"\1 orders per dag", out)
    out = out.replace(
        "(gerealiseerd plus ongerealiseerd)", "(gerealiseerd plus het verlies op open posities)"
    )
    out = re.sub(
        r"minimum van (\d+(?:\.\d+)?) EUR", lambda m: f"minimum van {eur(float(m[1]))}", out
    )
    out = re.sub(r"€ ?(\d+(?:\.\d+)?)", lambda m: eur(float(m[1])), out)
    return re.sub(
        r"(\d+)\.(\d+)%",
        lambda m: m[1] + ("" if not m[2].strip("0") else "," + m[2].rstrip("0")) + "%",
        out,
    )


def sentiment_pill(label: str) -> str:
    """Pill colour for a sentiment label. Neutral and mixed labels get the grey pill: blue
    is reserved for links and information."""
    if "positief" in label:
        return "pill-green"
    if "negatief" in label:
        return "pill-red"
    return ""


def action_pill(action: str | None) -> str:
    return {"buy": "pill-green", "sell": "pill-red", "hold": ""}.get(action or "", "")


def login_disabled(request: object) -> bool:
    """True when the app runs without its login (AUTH_ENABLED=false): every page warns."""
    state = getattr(getattr(request, "app", None), "state", None)
    return state is not None and "auth" in state._state and state.auth is None


def app_settings(request: object) -> object | None:
    """The running settings for templates that were rendered without a ``settings`` context
    (error pages): base.html still shows the mode and the footer."""
    state = getattr(getattr(request, "app", None), "state", None)
    if state is None:
        return None
    return state._state.get("settings")


def bot_paused(request: object) -> bool:
    """Whether the kill switch is on, for the navigation on every page. Reads the flag the
    engine checks (risk.is_paused); a page that cannot read it shows no pause."""
    state = getattr(getattr(request, "app", None), "state", None)
    engine = state._state.get("engine") if state is not None else None
    flag = getattr(engine, "paused", None)
    if isinstance(flag, bool):
        return flag
    if state is None or "engine" not in state._state:
        return False
    try:
        from app.db import SessionLocal
        from app.trading import risk

        with SessionLocal() as session:
            return risk.is_paused(session)
    except Exception:  # no table yet, database away: the page itself still renders
        return False


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(
    {
        "asset_version": ASSET_VERSION,
        "login_disabled": login_disabled,
        "app_settings": app_settings,
        "bot_paused": bot_paused,
        "modes_nl": MODES_NL,
        "live_banner": LIVE_BANNER,
        "kill_switch_text": KILL_SWITCH_NL,
        "close_reasons_nl": CLOSE_REASONS_NL,
        # The hard limits in one wording on every page (UX-105).
        "daily_loss_rule_nl": daily_loss_rule_nl,
        "max_orders_rule_nl": max_orders_rule_nl,
        "orders_limit_nl": orders_limit_nl,
        # The rules change but the free description does not (UX-12): every diff says so.
        "description_outdated": description_outdated,
        # Flashes for one section of the page (ui.flashes_here, UX-99).
        "section_flashes": take_flashes,
    }
)
templates.env.filters.update(
    {
        "nl": nl,
        "num": num,
        "eur": eur,
        "eur_short": eur_short,
        "pct": pct,
        "pct_limit": pct_limit,
        "pnl_class": pnl_class,
        "price": price,
        "price_eur": price_eur,
        "amount": amount,
        # Moments: <time> markup in UTC that app.js shows in local time. The *_text variants
        # are plain text, for attributes (title=, data-confirm=) and composed sentences.
        "dt": time_html,
        "isodt": time_html,
        "ts": time_html,
        "ago": ago_html,
        "dt_text": dt,
        "ago_text": ago,
        "md": md,
        "linkify": linkify,
        "close_reason": close_reason,
        "refusal": refusal_nl,
        "capfirst": capfirst,
        "hours_nl": hours_nl,
        "market_list": market_list,
        "order_reason": order_reason,
        "indicator_nl": indicator_nl,
        "condition_nl": condition_nl,
        "compact": format_value,
        "sentiment_class": sentiment_class,
        "sentiment_pill": sentiment_pill,
        "action_pill": action_pill,
        # Backwards compatibility with the old templates' epoch filter.
        "datetimeformat": time_html,
    }
)
