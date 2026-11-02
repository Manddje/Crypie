"""Helpers for the strategy page: plain-Dutch validation errors, a readable backtest, the
confirmation before a save, and a short-lived store for results (Post/Redirect/Get).

Nothing here changes a strategy; main.py saves through app.strategy.store.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.strategy.engine import BacktestResult, Candle
from app.strategy.model import Strategy

MONTHS_NL = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}
# Fewer closed trades than this in a backtest: too few to judge the strategy on.
FEW_TRADES = 5
# Live buy signal on more candles than this: almost every cycle asks Claude and buys.
LOOSE_BUY_SIGNAL_PCT = 20.0


# -- Results of a test or a proposal, kept briefly for the redirect ------------------------


class ResultStore:
    """The outcome of 'Testen' or 'Voorstel maken', kept in memory for a while so the form can
    redirect to ``/strategy?result=<id>``: a reload shows the result again instead of sending
    the form (and a paid Claude call) once more. One process serves the app (--workers 1); a
    restart forgets the results, which the page then says."""

    def __init__(self, max_items: int = 30, ttl_s: float = 2 * 3600):
        self.max_items = max_items
        self.ttl_s = ttl_s
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def _prune(self, now: float) -> None:
        for key in [k for k, (at, _) in self._items.items() if now - at > self.ttl_s]:
            del self._items[key]
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def put(self, data: dict[str, Any]) -> str:
        now = time.monotonic()
        token = secrets.token_urlsafe(12)
        self._items[token] = (now, data)
        self._prune(now)
        return token

    def get(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        self._prune(time.monotonic())
        item = self._items.get(token)
        return item[1] if item else None


RESULTS = ResultStore()
RESULT_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


# -- Validation errors in plain Dutch ------------------------------------------------------

FIELDS_NL = {
    "name": "Naam",
    "description": "Omschrijving",
    "market": "Backtest-markt",
    "interval": "Candles",
    "indicators": "Indicatoren",
    "buy": "Koopregels",
    "sell": "Verkoopregels",
    "stop_loss_pct": "Stop-loss",
    "trailing_stop_pct": "Trailing stop",
    "trailing_start_pct": "Trailing stop actief vanaf winst",
    "take_profit_pct": "Take-profit",
    "position_size_pct": "Positiegrootte",
}
CONDITION_PARTS_NL = {
    "left": "linkerkant",
    "op": "vergelijking",
    "right": "rechterkant",
    "soft": "zacht filter",
}
INDICATOR_PARTS_NL = {
    "id": "id",
    "type": "type",
    "period": "periode",
    "fast": "snelle periode",
    "slow": "trage periode",
    "signal": "signaalperiode",
    "std_dev": "standaardafwijking",
    "tolerance_pct": "marge (%)",
}
# Pydantic adds the member of a union to the path ('right' -> 'str' / 'constrained-float').
_UNION_MEMBERS = ("str", "float", "int", "constrained-float", "constrained-int", "bool")

_VALUE_ERRORS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"unknown series '([^']*)'; available: (.+)"),
        "onbekende reeks '{0}'. Beschikbaar: {1}",
    ),
    (
        re.compile(r"macd fast period must be smaller than slow period"),
        "de snelle periode (fast) moet kleiner zijn dan de trage periode (slow)",
    ),
    (re.compile(r"indicator ids must be unique"), "elke indicator-id mag maar één keer voorkomen"),
    (
        re.compile(
            r"indicators and look-backs need (\d+) candles of warm-up; at most (\d+) fit in the "
            r"(\d+) candles Bitvavo returns"
        ),
        "de indicatoren en terugblikken hebben {0} candles opwarmtijd nodig; er passen er "
        "hoogstens {1} in de {2} candles die Bitvavo teruggeeft",
    ),
    (
        re.compile(r"(rising|falling) needs a whole number of candles \(1 or more\) as right"),
        "bij '{0}' is de rechterkant een geheel aantal candles (1 of meer)",
    ),
    (
        re.compile(r"(rising|falling) can look back at most (\d+) candles"),
        "'{0}' kan hoogstens {1} candles terugkijken",
    ),
    (
        re.compile(r"left must be a series name, got (.+)"),
        "de linkerkant moet een reeksnaam zijn, niet {0}",
    ),
)


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace(".", ",")


def path_nl(loc: tuple) -> tuple[str, str]:
    """('buy', 0, 'op') -> ('Koopregel 1 → vergelijking', 'buy[0].op')."""
    parts = [p for p in loc if p not in _UNION_MEMBERS]
    if not parts:
        return "Strategie", ""
    raw = ""
    for p in parts:
        raw += f"[{p}]" if isinstance(p, int) else (f".{p}" if raw else str(p))
    head = parts[0]
    if head in ("buy", "sell") and len(parts) >= 2 and isinstance(parts[1], int):
        text = f"{'Koopregel' if head == 'buy' else 'Verkoopregel'} {parts[1] + 1}"
        if len(parts) >= 3:
            text += " → " + CONDITION_PARTS_NL.get(str(parts[2]), str(parts[2]))
        return text, raw
    if head == "indicators" and len(parts) >= 2 and isinstance(parts[1], int):
        text = f"Indicator {parts[1] + 1}"
        rest = parts[2:]
        if rest and rest[0] in INDICATOR_TYPES:
            text += f" ({rest[0]})"
            rest = rest[1:]
        if rest:
            text += " → " + INDICATOR_PARTS_NL.get(str(rest[0]), str(rest[0]))
        return text, raw
    return FIELDS_NL.get(str(head), str(head)), raw


INDICATOR_TYPES = (
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "drawdown",
    "fibonacci",
    "ema_support",
)


def message_nl(err: dict[str, Any]) -> str:
    """One pydantic error as a plain Dutch sentence part."""
    kind = str(err.get("type", ""))
    ctx = err.get("ctx") or {}
    loc = err.get("loc") or ()
    if kind == "missing":
        return "ontbreekt"
    if kind == "string_too_short":
        return "mag niet leeg zijn"
    if kind == "string_too_long":
        return f"is te lang (maximaal {ctx.get('max_length')} tekens)"
    if kind == "string_pattern_mismatch":
        if loc and loc[-1] == "market":
            return "moet een euromarkt zijn in hoofdletters, zoals BTC-EUR"
        if loc and loc[-1] == "id":
            return (
                "moet met een kleine letter beginnen, daarna alleen kleine letters, cijfers of _ "
                "(hoogstens 32 tekens)"
            )
        return "heeft een ongeldige vorm"
    if kind == "literal_error":
        expected = str(ctx.get("expected", "")).replace(" or ", ", ")
        return f"kies een van: {expected}"
    if kind == "union_tag_invalid":
        return (
            f"onbekend indicatortype '{ctx.get('tag')}'; kies een van: {ctx.get('expected_tags')}"
        )
    if kind == "union_tag_not_found":
        return 'het veld type ontbreekt (bijvoorbeeld "type": "rsi")'
    if kind in ("int_parsing", "int_type", "int_from_float"):
        return "moet een geheel getal zijn"
    if kind in ("float_parsing", "float_type"):
        return "moet een getal zijn"
    if kind == "finite_number":
        return "moet een gewoon getal zijn (geen NaN of oneindig)"
    if kind in ("bool_parsing", "bool_type"):
        return "moet true of false zijn"
    if kind in ("string_type", "str_type"):
        return "moet tekst tussen dubbele aanhalingstekens zijn"
    if kind in ("model_type", "dict_type", "model_attributes_type"):
        return "moet een object zijn: { … }"
    if kind == "list_type":
        return "moet een lijst zijn: [ … ]"
    bounds = {
        "greater_than": ("groter dan", "gt"),
        "greater_than_equal": ("minstens", "ge"),
        "less_than": ("kleiner dan", "lt"),
        "less_than_equal": ("hoogstens", "le"),
    }
    if kind in bounds:
        word, key = bounds[kind]
        return f"moet {word} {_num(ctx.get(key))} zijn"
    if kind == "too_short":
        n = ctx.get("min_length")
        return f"moet minstens {n} {'regel' if n == 1 else 'regels'} bevatten"
    if kind == "too_long":
        return f"mag hoogstens {ctx.get('max_length')} regels bevatten"
    if kind == "value_error":
        text = str(ctx.get("error") or err.get("msg", "")).removeprefix("Value error, ")
        for pattern, template in _VALUE_ERRORS:
            if m := pattern.fullmatch(text):
                return template.format(*m.groups())
        return text
    return str(err.get("msg", "is ongeldig"))


def validation_errors_nl(exc: ValidationError) -> list[dict[str, str]]:
    """Every error once, with where it is ('Koopregel 1 → vergelijking'), the JSON path and
    what to do. A value that fits no member of a union ('right') is reported once."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for err in exc.errors():
        where, raw = path_nl(tuple(err.get("loc") or ()))
        union = any(p in _UNION_MEMBERS for p in err.get("loc") or ())
        message = (
            "moet een reeksnaam (tekst) of een getal zijn"
            if union and raw.endswith(".right")
            else message_nl(err)
        )
        key = f"{raw}:{message}" if not union else raw
        if key in seen:
            continue
        seen.add(key)
        out.append({"where": where, "path": raw, "message": message})
    return out


JSON_MESSAGES_NL = (
    ("Illegal trailing comma", "er staat een komma te veel vóór de afsluitende } of ]"),
    ("Expecting property name enclosed in double quotes", "hier verwacht ik een veldnaam tussen "
     "dubbele aanhalingstekens; staat er een komma te veel vóór een } of ]?"),
    ("Expecting value", "hier verwacht ik een waarde: een getal, tekst tussen dubbele "
     "aanhalingstekens, true, false, null, een lijst of een object"),
    ("Expecting ',' delimiter", "hier ontbreekt een komma tussen twee waarden"),
    ("Expecting ':' delimiter", "hier ontbreekt de dubbele punt na een veldnaam"),
    ("Unterminated string", "een tekst mist het afsluitende aanhalingsteken"),
    ("Extra data", "na het einde van de JSON staat nog tekst"),
    ("Invalid control character", "een tekst bevat een ongeldig teken, zoals een harde "
     "regelovergang"),
    ("Invalid \\escape", "een tekst bevat een ongeldige \\-code"),
    ("Invalid \\u", "een tekst bevat een ongeldige \\u-code"),
)  # fmt: skip


class StrategyInputError(ValueError):
    """The editor's text is no valid strategy. ``errors`` are rows for the page; ``context`` is
    the part of the JSON around a syntax error."""

    def __init__(self, errors: list[dict[str, str]], context: list[dict] | None = None):
        self.errors = errors
        self.context = context or []
        super().__init__("; ".join(f"{e['where']}: {e['message']}" for e in errors))


def _reject_constant(name: str) -> float:
    raise ValueError(name)


def _json_context(text: str, line: int, column: int) -> list[dict[str, Any]]:
    """The line with the error and one line around it, for a <pre> under the editor."""
    lines = text.splitlines() or [""]
    out = []
    for n in range(max(1, line - 1), min(len(lines), line + 1) + 1):
        out.append({"n": n, "text": lines[n - 1][:200], "here": n == line, "column": column})
    return out


def parse_strategy_json(text: str) -> Strategy:
    """The strategy in ``text``; raises StrategyInputError with Dutch errors."""
    try:
        json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        reason = next((nl for en, nl in JSON_MESSAGES_NL if exc.msg.startswith(en)), exc.msg)
        where = f"Regel {exc.lineno}, kolom {exc.colno}"
        raise StrategyInputError(
            [{"where": where, "path": "", "message": f"de JSON klopt niet: {reason}"}],
            _json_context(text, exc.lineno, exc.colno),
        ) from exc
    except ValueError as exc:  # NaN, Infinity
        raise StrategyInputError(
            [
                {
                    "where": "Strategie",
                    "path": "",
                    "message": f"{exc} is geen geldig getal; gebruik een gewoon getal",
                }
            ]
        ) from exc
    try:
        return Strategy.model_validate_json(text)
    except ValidationError as exc:
        raise StrategyInputError(validation_errors_nl(exc)) from exc


# -- The backtest, easy to judge -----------------------------------------------------------


def _date_nl(ms: int) -> str:
    moment = datetime.fromtimestamp(ms / 1000, UTC)
    return f"{moment.day} {MONTHS_NL[moment.month - 1]}"


def period_nl(first_ms: int | None, last_ms: int | None, candles: int, interval: str) -> str:
    """'≈ 21 dagen, 12 okt – 2 nov'."""
    minutes = INTERVAL_MINUTES.get(interval, 60) * candles
    if minutes >= 2 * 1440:
        span = f"≈ {round(minutes / 1440)} dagen"
    else:
        span = f"≈ {round(minutes / 60)} uur"
    if first_ms and last_ms:
        return f"{span}, {_date_nl(first_ms)} – {_date_nl(last_ms)}"
    return span


def equity_curves(
    candles: list[Candle], result: BacktestResult, position_size_pct: float
) -> tuple[list[float], list[float]]:
    """The value of the test account per candle for the strategy and for buying and holding,
    rebuilt from the trades with the backtest's own rules (fees on both sides, the position
    size, no buy on the candle of a sale)."""
    if not candles:
        return [], []
    fee = result.fee_pct / 100
    eur, coin = result.start_eur, 0.0
    trades = list(result.trades)
    t = 0
    holding = None
    first_close = candles[0][4]
    strategy_values: list[float] = []
    hold_values: list[float] = []
    for candle in candles:
        ts, close = int(candle[0]), candle[4]
        if holding is not None:
            if holding.exit_time is not None and holding.exit_time == ts:
                eur += coin * (holding.exit_price or close) * (1 - fee)
                coin, holding = 0.0, None
        elif t < len(trades) and trades[t].entry_time == ts:
            holding = trades[t]
            t += 1
            eur -= eur * position_size_pct / 100
            coin = holding.amount
        strategy_values.append(eur + coin * close)
        hold_values.append(result.start_eur * close / first_close if first_close else 0.0)
    return strategy_values, hold_values


def _sample(values: list[float], points: int) -> list[float]:
    if len(values) <= points:
        return values
    step = (len(values) - 1) / (points - 1)
    return [values[round(i * step)] for i in range(points)]


def curve_svg(strategy: list[float], hold: list[float], width: int = 600, height: int = 120):
    """Polyline points for both curves on one scale, plus the scale's low and high."""
    a, b = _sample(strategy, 160), _sample(hold, 160)
    if len(a) < 2:
        return None
    low, high = min(a + b), max(a + b)
    span = (high - low) or 1.0

    def line(values: list[float]) -> str:
        n = len(values) - 1
        return " ".join(
            f"{i / n * width:.1f},{height - 4 - (v - low) / span * (height - 8):.1f}"
            for i, v in enumerate(values)
        )

    return {
        "strategy": line(a),
        "hold": line(b),
        "low": low,
        "high": high,
        "width": width,
        "height": height,
        "strategy_low": min(strategy),
    }


def backtest_view(bt: dict[str, Any]) -> dict[str, Any]:
    """What the page says about a backtest: the period, a verdict against buying and holding,
    and the warnings (no trades, few trades, a buy signal on most candles)."""
    trades = int(bt.get("trades") or 0)
    open_position = bool(bt.get("open_position"))
    ret, hold = float(bt.get("return_pct") or 0), float(bt.get("buy_hold_return_pct") or 0)
    view: dict[str, Any] = {
        "period": period_nl(
            bt.get("first_time"),
            bt.get("last_time"),
            int(bt.get("candles") or 0),
            bt.get("interval", "1h"),
        ),
        "no_trades": trades == 0 and not open_position,
        "few_trades": 0 < trades < FEW_TRADES or (trades == 0 and open_position),
        "difference": round(ret - hold, 2),
        "better": ret >= hold,
        "wins": bt.get("wins"),
        "loose": (bt.get("live_buy_signal_pct") or 0) > LOOSE_BUY_SIGNAL_PCT,
    }
    return view


def save_confirm(
    name: str, markets: list[str], bt: dict[str, Any] | None, live: bool, tested: bool = True
) -> str | None:
    """The question before 'opslaan': always in live, and whenever the backtest gives a reason
    to hesitate. None means no question."""
    from app.web import pct  # the page's own number format

    reasons = []
    if not tested:
        reasons.append("deze versie is niet getest")
    if bt:
        view = backtest_view(bt)
        if view["no_trades"]:
            reasons.append("de backtest deed geen enkele trade")
        elif not view["better"]:
            reasons.append(
                "de backtest deed het slechter dan kopen en vasthouden "
                f"({pct(bt.get('return_pct'))} tegenover {pct(bt.get('buy_hold_return_pct'))})"
            )
        if view["loose"]:
            reasons.append(
                "live geeft deze strategie op "
                f"{pct(bt.get('live_buy_signal_pct'), 1, False)} van de candles een koopsignaal"
            )
    if not reasons and not live:
        return None
    text = f"Strategie '{name}' opslaan en meteen actief maken op {', '.join(markets)}?"
    if reasons:
        text += " Let op: " + "; ".join(reasons) + "."
    return ("LIVE · echt geld: " + text) if live else text


# Fields of a strategy that decide when the bot buys or sells.
RULE_FIELDS = (
    "indicators",
    "buy",
    "sell",
    "stop_loss_pct",
    "trailing_stop_pct",
    "trailing_start_pct",
    "take_profit_pct",
)


def description_outdated(diff: list[dict[str, str]] | None) -> bool:
    """The rules change but the free description stays: it may now contradict them."""
    fields = {d["field"] for d in diff or []}
    return bool(fields & set(RULE_FIELDS)) and "description" not in fields
