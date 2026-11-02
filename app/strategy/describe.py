"""Dutch wording for strategy conditions, shared by the pages and the trading journal."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.engine import Series, _value, lookback_value
from app.strategy.model import LOOKBACK_OPS, Condition, Strategy

OPERATORS_NL = {
    "<": "lager dan",
    "<=": "hoogstens",
    ">": "hoger dan",
    ">=": "minstens",
    "crosses_above": "kruist omhoog door",
    "crosses_below": "kruist omlaag door",
    "rising": "stijgt t.o.v.",
    "falling": "daalt t.o.v.",
}

SERIES_NL = {
    "open": "openingskoers",
    "high": "hoogste koers",
    "low": "laagste koers",
    "close": "koers",
    "volume": "volume",
}


def series_nl(name: str | float) -> str:
    if isinstance(name, int | float):
        return format_value(float(name), threshold=True)
    return SERIES_NL.get(name, name)


def format_value(value: float, threshold: bool = False) -> str:
    """Compact Dutch number: 69.498 for prices, 52,2 for oscillators, 0,0012 for small values.

    A ``threshold`` written by the user (``35``) keeps its own precision.
    """
    a = abs(value)
    if threshold and value == int(value):
        decimals = 0
    else:
        decimals = 0 if a >= 1000 else 1 if a >= 10 else 2 if a >= 1 else 4
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def condition_nl(cond: Condition) -> str:
    """'rsi14 lager dan 30', 'koers kruist omhoog door ema50 (zacht filter)'."""
    if cond.op in LOOKBACK_OPS:
        n = int(cond.right)
        right = "1 candle eerder" if n == 1 else f"{n} candles eerder"
    else:
        right = series_nl(cond.right)
    text = f"{series_nl(cond.left)} {OPERATORS_NL.get(cond.op, cond.op)} {right}"
    return f"{text} (zacht filter)" if cond.soft else text


@dataclass
class ConditionCheck:
    """One strategy condition evaluated on the latest candle, with the values it compared."""

    text: str  # the rule in Dutch
    left: float | None
    right: float | None
    ok: bool
    soft: bool = False

    @property
    def values_nl(self) -> str:
        if self.left is None or self.right is None:
            return "nog niet genoeg data"
        return f"{format_value(self.left)} t.o.v. {format_value(self.right)}"

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "left": self.left,
            "right": self.right,
            "ok": self.ok,
            "soft": self.soft,
        }


def check_conditions(
    conditions: list[Condition], series: dict[str, Series], i: int
) -> list[ConditionCheck]:
    from app.strategy.engine import check_condition

    out = []
    for cond in conditions:
        left = _value(series, cond.left, i)
        if cond.op in LOOKBACK_OPS:
            right = lookback_value(cond, series, i)  # show what it is compared with
        else:
            right = _value(series, cond.right, i)
        out.append(
            ConditionCheck(
                condition_nl(cond), left, right, check_condition(cond, series, i), cond.soft
            )
        )
    return out


def explain_signal(strategy: Strategy, series: dict[str, Series], i: int) -> dict[str, list]:
    """Buy and sell conditions with their outcome on candle ``i``; shown on /trading."""
    return {
        "buy": check_conditions(strategy.buy, series, i),
        "sell": check_conditions(strategy.sell, series, i),
    }


def signal_reason_nl(signal: str, checks: dict[str, list[ConditionCheck]]) -> str:
    """One Dutch sentence on why the strategy gave this signal, with the actual values."""

    def describe(items: list[ConditionCheck]) -> str:
        return (
            "; ".join(f"{c.text} {'✓' if c.ok else '✗'} ({c.values_nl})" for c in items)
            or "geen voorwaarden"
        )

    if signal == "buy":
        soft_failed = [c for c in checks["buy"] if c.soft and not c.ok]
        note = (
            f" {len(soft_failed)} zacht filter niet gehaald, Claude weegt dat mee."
            if len(soft_failed) == 1
            else f" {len(soft_failed)} zachte filters niet gehaald, Claude weegt dat mee."
            if soft_failed
            else ""
        )
        return f"Koopsignaal: {describe(checks['buy'])}.{note}"
    if signal == "sell":
        return f"Verkoopsignaal: {describe(checks['sell'])}."
    hard = [c for c in checks["buy"] if not c.soft] or checks["buy"]
    failed = [c for c in hard if not c.ok]
    if not checks["buy"]:
        return "Geen koopsignaal: de strategie heeft geen koopvoorwaarden."
    return (
        f"Geen koopsignaal, {len(failed)} van {len(hard)} harde koopvoorwaarden niet gehaald: "
        f"{describe(checks['buy'])}."
    )


def levels_summary(strategy: Strategy, values: dict[str, float | None]) -> list[dict]:
    """Support and resistance from the strategy's fibonacci / ema_support indicators.

    ``values`` is a flat ``{series: value}`` map (the latest candle, or a journal entry's
    stored indicators). One entry per indicator with its numbers and a Dutch sentence; used as
    Claude's ``support_levels`` context and on the /trading page.
    """
    out: list[dict] = []
    for ind in strategy.indicators:
        if ind.type == "fibonacci":
            retrace = values.get(ind.id)
            up = values.get(f"{ind.id}_up")
            if retrace is None or up is None:
                continue
            upward = up > 0
            lv = {k: values.get(f"{ind.id}_{k}") for k in ("382", "500", "618", "high", "low")}
            near = values.get(f"{ind.id}_near")
            if upward:
                swing = "opwaartse swing"
                levels_nl = "steunniveaus"
            else:
                swing = "neerwaartse swing"
                levels_nl = "weerstandsniveaus"
            text = (
                f"Fibonacci ({ind.period} candles): {swing} van {format_value(lv['low'])} naar "
                f"{format_value(lv['high'])}, koers heeft {format_value(retrace)}% teruggelopen; "
                f"{levels_nl} 38,2% {format_value(lv['382'])}, 50% {format_value(lv['500'])}, "
                f"61,8% {format_value(lv['618'])}"
            )
            if near is not None:
                text += f"; dichtstbijzijnde niveau op {format_value(near)}%"
            if upward and 38.2 <= retrace <= 61.8:
                text += " (in de golden pocket, klassieke koopzone)"
            elif upward and retrace > 78.6:
                text += " (bijna hele stijging teruggegeven: steun houdt niet)"
            out.append(
                {
                    "type": "fibonacci",
                    "id": ind.id,
                    "period": ind.period,
                    "upward_swing": upward,
                    "retracement_pct": retrace,
                    "swing_high": lv["high"],
                    "swing_low": lv["low"],
                    "levels": {"38.2": lv["382"], "50": lv["500"], "61.8": lv["618"]},
                    "nearest_level_distance_pct": near,
                    "in_golden_pocket": bool(upward and 38.2 <= retrace <= 61.8),
                    "text_nl": text,
                }
            )
        elif ind.type == "ema_support":
            dist = values.get(ind.id)
            ema_value = values.get(f"{ind.id}_ema")
            bounce = values.get(f"{ind.id}_bounce")
            if dist is None or ema_value is None:
                continue
            where = f"{format_value(abs(dist))}% {'boven' if dist >= 0 else 'onder'}"
            text = f"EMA{ind.period} ({format_value(ema_value)}): koers {where}"
            if bounce:
                text += f"; steun getest binnen {ind.tolerance_pct:g}% en gehouden"
            elif dist < -ind.tolerance_pct:
                text += "; koers onder de EMA, steun gebroken"
            out.append(
                {
                    "type": "ema_support",
                    "id": ind.id,
                    "period": ind.period,
                    "ema": ema_value,
                    "distance_pct": dist,
                    "bounce": bool(bounce),
                    "tolerance_pct": ind.tolerance_pct,
                    "text_nl": text,
                }
            )
    return out


DIFF_FIELDS_NL: dict[str, str] = {
    "name": "Naam",
    "market": "Markt",
    "interval": "Candles",
    "indicators": "Indicatoren",
    "buy": "Kopen als",
    "sell": "Verkopen als",
    "stop_loss_pct": "Stop-loss (%)",
    "trailing_stop_pct": "Trailing stop (%)",
    "trailing_start_pct": "Trailing stop actief vanaf winst (%)",
    "take_profit_pct": "Take-profit (%)",
    "position_size_pct": "Positiegrootte (%)",
    "description": "Omschrijving",
}


def _indicator_text(ind: object) -> str:
    params = ", ".join(
        f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}"
        for k, v in ind.model_dump().items()  # type: ignore[attr-defined]
        if k not in ("id", "type")
    )
    return f"{ind.id}: {ind.type}({params})"  # type: ignore[attr-defined]


def _field_text(strategy: Strategy, key: str) -> str:
    value = getattr(strategy, key)
    if key == "indicators":
        return "; ".join(_indicator_text(i) for i in value)
    if key in ("buy", "sell"):
        return " én ".join(condition_nl(c) for c in value) or "–"
    if value is None:
        return "geen"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def strategy_diff(old: Strategy, new: Strategy) -> list[dict[str, str]]:
    """Field by field what changes when ``new`` replaces ``old``, in Dutch, so the owner can
    review a proposal before applying it. Unchanged fields are left out."""
    out: list[dict[str, str]] = []
    for key, label in DIFF_FIELDS_NL.items():
        before, after = _field_text(old, key), _field_text(new, key)
        if before != after:
            out.append({"field": key, "label": label, "old": before, "new": after})
    return out
