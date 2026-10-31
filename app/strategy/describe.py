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
