"""What the Handelen page shows: decisions as one readable summary plus their checklist, the
log of cycles in pages, open positions with their result and distance to the stop, and the
messages after an action.

Read-only: nothing here changes the database or calls Bitvavo or Claude.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import Decision, Position
from app.strategy.describe import OPERATORS_NL, ConditionCheck, signal_reason_nl
from app.strategy.model import Strategy

# Rows per page in "Alle cycli": enough to scan a day, never thousands of rows.
CYCLES_PER_PAGE = 25
CYCLE_VIEWS = ("important", "all")
SOFT_SUFFIX = " (zacht filter)"
# Mechanical exits are journaled as 'stop_loss geraakt op 66091.50' (engine._mechanical_exit).
_MECHANICAL = re.compile(r"^(stop_loss|trailing_stop|take_profit) geraakt op ([0-9]+(?:\.[0-9]+)?)")
# The news sentence of a rules decision: 'Nieuws licht negatief (-0.17, 2 nieuwsfeiten uit 2
# berichten, 1 met impact middel of groot). ' or 'Geen beoordeeld nieuws. ' (engine, step 2).
_NEWS = re.compile(r"^(?:Geen beoordeeld nieuws|Nieuws [^()]+ \([+-][0-9.]+, ([^()]*)\))\.\s*")


# -- names -------------------------------------------------------------------------------------


def model_name_nl(model_id: str | None) -> str:
    """A model id as people say it: 'claude-fable-5-1' -> 'Claude Fable 5.1',
    'claude-3-5-haiku-20241022' -> 'Claude 3.5 Haiku'. Unknown shapes come back as they are."""
    if not model_id:
        return "Claude"
    words: list[str] = []
    digits: list[str] = []
    for part in str(model_id).split("-"):
        if re.fullmatch(r"[0-9]{8}", part) or part == "latest":
            continue  # a release date or alias, not part of the name
        if part.isdigit():
            digits.append(part)
            continue
        if digits:
            words.append(".".join(digits))
            digits = []
        words.append(part[:1].upper() + part[1:])
    if digits:
        words.append(".".join(digits))
    return " ".join(words) or str(model_id)


def indicator_labels(strategy: Strategy) -> dict[str, str]:
    """Indicator id -> readable name ('rsi14' -> 'RSI 14'), as on Overzicht and Strategie."""
    from app.web import indicator_nl

    return {ind.id: indicator_nl(ind) for ind in strategy.indicators}


def readable_conditions(text: str | None, labels: dict[str, str]) -> str:
    """Several conditions joined with '; ' (the dip watch), each made readable."""
    if not text:
        return ""
    return "; ".join(readable_condition(part, labels) for part in str(text).split("; "))


def readable_condition(text: str, labels: dict[str, str]) -> str:
    """'rsi14 lager dan 35' -> 'RSI 14 lager dan 35': the indicator ids on either side of the
    operator get their readable name; everything else stays as it was journaled."""
    if not labels:
        return text
    soft = text.endswith(SOFT_SUFFIX)
    core = text[: -len(SOFT_SUFFIX)] if soft else text
    for op in sorted(OPERATORS_NL.values(), key=len, reverse=True):
        sep = f" {op} "
        if sep in core:
            left, right = core.split(sep, 1)
            core = f"{labels.get(left, left)}{sep}{labels.get(right, right)}"
            break
    return core + (SOFT_SUFFIX if soft else "")


# -- one decision ------------------------------------------------------------------------------


@dataclass
class Checklist:
    """What ui.checks needs: ``signal`` picks the side ('sell' shows the sell conditions,
    anything else the buy conditions) and ``check_list()`` gives the conditions."""

    signal: str
    rules: dict[str, list[dict]]

    def check_list(self) -> dict[str, list[dict]]:
        return self.rules


@dataclass
class DecisionView:
    """A journaled decision as the page shows it: the summary first, then the checklist and the
    explanation, without repeating the conditions in the text."""

    d: Decision
    side: str  # "buy" or "sell": the conditions that matter for this decision
    summary: str | None
    checklist: Checklist
    has_checks: bool
    explanation: str | None
    news_detail: str | None
    news_score: str | None
    important: bool
    outcomes: list[dict[str, Any]] = field(default_factory=list)


def is_important(d: Decision) -> bool:
    """Shown in the default 'Belangrijk' view: anything but a quiet 'afwachten' by the rules."""
    return bool(
        d.action != "hold"
        or d.blocked_by
        or d.source in ("claude", "mechanical")
        or d.signal == "buy"
    )


def _important_clause():
    return or_(
        Decision.action != "hold",
        Decision.blocked_by.is_not(None),
        Decision.source.in_(("claude", "mechanical")),
        Decision.signal == "buy",
    )


def _side(d: Decision) -> str:
    if d.signal == "sell" or (d.has_position and d.signal != "buy"):
        return "sell"
    return "buy"


def _summary(d: Decision, side: str, rules: list[dict]) -> str | None:
    """'1 van 2 koopvoorwaarden gehaald', 'koopsignaal: alle 2 koopvoorwaarden gehaald · 1 zacht
    filter niet gehaald'. Shown right after the action ('Afwachten · …')."""
    if not rules:
        return None
    hard = [r for r in rules if not r.get("soft")] or rules
    met = sum(1 for r in hard if r.get("ok"))
    n = len(hard)
    word = ("koopvoorwaarde" if side == "buy" else "verkoopvoorwaarde") + ("" if n == 1 else "n")
    text = f"alle {n} {word} gehaald" if met == n and n > 1 else f"{met} van {n} {word} gehaald"
    if d.signal == side:
        text = ("koopsignaal: " if side == "buy" else "verkoopsignaal: ") + text
    soft_failed = sum(1 for r in rules if r.get("soft") and not r.get("ok"))
    if soft_failed:
        text += f" · {soft_failed} " + (
            "zacht filter niet gehaald" if soft_failed == 1 else "zachte filters niet gehaald"
        )
    return text


def _signal_sentence(d: Decision, checks: dict[str, list[dict]]) -> str | None:
    """The sentence the engine wrote in front of a rules decision (signal_reason_nl), rebuilt
    from the stored checks, so the page can leave it out: the checklist shows the same."""
    try:
        objs = {
            side: [
                ConditionCheck(
                    str(c.get("text", "")),
                    c.get("left"),
                    c.get("right"),
                    bool(c.get("ok")),
                    bool(c.get("soft")),
                )
                for c in checks.get(side, [])
            ]
            for side in ("buy", "sell")
        }
        return signal_reason_nl(d.signal, objs)
    except (TypeError, ValueError):
        return None


def _explain(d: Decision, checks: dict[str, list[dict]]) -> tuple[str | None, str | None]:
    """(explanation, news details) for the page. Rules decisions lose the signal sentence (the
    checklist shows it) and their news sentence (the news line shows it); Claude's reasoning is
    shown as Claude wrote it (escaped by the template)."""
    text = (d.reasoning or "").strip()
    if not text:
        return None, None
    if d.source == "mechanical":
        from app.web import CLOSE_REASONS_NL, price_eur

        m = _MECHANICAL.match(text)
        if m:
            what = CLOSE_REASONS_NL.get(m.group(1), m.group(1))
            rest = text[m.end() :].strip()
            sentence = f"{what[:1].upper()}{what[1:]} op {price_eur(float(m.group(2)))}"
            return (f"{sentence} {rest}".strip(), None)
        return text, None
    if d.source != "rules":
        return text, None
    # Decisions stored before the wording changed.
    text = text.replace(
        "Claude niet beschikbaar (ANTHROPIC_API_KEY ontbreekt)",
        "Claude niet beschikbaar: er is geen Claude-sleutel",
    )
    sentence = _signal_sentence(d, checks)
    if sentence and text.startswith(sentence):
        text = text[len(sentence) :].lstrip()
    news = None
    m = _NEWS.match(text)
    if m:
        # Older decisions said '1 nieuwsfeiten uit 1 berichten' and 'N van hoge impact' (the
        # count is impact 2 and up: 'middel of groot', as on Nieuws and the plan; UX-105).
        news = re.sub(r"\b1 (nieuwsfeit|bericht)en\b", r"1 \1", m.group(1) or "")
        news = re.sub(r"\b(\d+) van hoge impact\b", r"\1 met impact middel of groot", news) or None
        text = text[m.end() :].lstrip()
    return (text or None), news


def _outcomes(d: Decision) -> list[dict[str, Any]]:
    out = []
    for label, later in (("Na 1u", d.price_1h), ("Na 24u", d.price_24h), ("Na 72u", d.price_72h)):
        pct = (later / d.price - 1) * 100 if later and d.price else None
        out.append({"label": label, "pct": pct})
    return out


def decision_view(d: Decision, labels: dict[str, str] | None = None) -> DecisionView:
    try:
        raw = d.check_list()
    except (ValueError, TypeError):
        raw = {"buy": [], "sell": []}
    labels = labels or {}
    checks = {
        side: [
            {**c, "text": readable_condition(str(c.get("text", "")), labels)}
            for c in raw.get(side, [])
            if isinstance(c, dict)
        ]
        for side in ("buy", "sell")
    }
    side = _side(d)
    rules = checks[side]
    explanation, news_detail = _explain(d, raw)
    if d.source == "mechanical":
        summary = explanation
        explanation = None
    else:
        summary = _summary(d, side, rules)
    score = None
    if d.news_score is not None and d.news_label and d.news_label != "geen nieuws":
        score = f"{d.news_score:+.2f}".replace(".", ",")
    return DecisionView(
        d=d,
        side=side,
        summary=summary,
        checklist=Checklist("sell" if side == "sell" else "buy", checks),
        has_checks=bool(rules),
        explanation=explanation,
        news_detail=news_detail,
        news_score=score,
        important=is_important(d),
        outcomes=_outcomes(d),
    )


# -- the log of cycles -------------------------------------------------------------------------


def _cycles_url(view: str, market: str | None, page: int = 1) -> str:
    params: dict[str, Any] = {}
    if view != "important":
        params["view"] = view
    if market:
        params["market"] = market
    if page > 1:
        params["page"] = page
    return "/trading" + (f"?{urlencode(params)}" if params else "") + "#cycli"


def cycle_log(
    session: Session,
    mode: str,
    *,
    days: int,
    view: str = "important",
    market: str | None = None,
    page: int = 1,
    markets: list[str] | tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    per_page: int = CYCLES_PER_PAGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One page of the decisions of the last ``days`` days, newest first.

    ``view`` "important" (the default) leaves out the quiet 'afwachten' decisions of the rules;
    "all" shows everything. Counts for both views come along for the toggle.
    """
    view = view if view in CYCLE_VIEWS else "important"
    now = now or datetime.now(UTC)
    base = [Decision.mode == mode, Decision.created_at >= now - timedelta(days=days)]
    in_period = list(base)
    if market:
        base.append(Decision.market == market)

    def count(*extra) -> int:
        stmt = select(func.count(Decision.id)).where(*base, *extra)
        return int(session.scalar(stmt) or 0)

    total_all = count()
    total_important = count(_important_clause())
    total = total_important if view == "important" else total_all
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    stmt = select(Decision).where(*base)
    if view == "important":
        stmt = stmt.where(_important_clause())
    rows = session.scalars(
        stmt.order_by(Decision.id.desc()).offset((page - 1) * per_page).limit(per_page)
    ).all()
    seen = session.scalars(select(Decision.market).where(*in_period).distinct()).all()
    choices = list(markets) + sorted(m for m in seen if m not in markets)
    if market and market not in choices:
        choices.append(market)
    return {
        "rows": [decision_view(d, labels) for d in rows],
        "view": view,
        "market": market,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "total_all": total_all,
        "total_important": total_important,
        "first": (page - 1) * per_page + 1 if total else 0,
        "last": min(page * per_page, total),
        "view_links": {v: _cycles_url(v, market) for v in CYCLE_VIEWS},
        "market_links": [
            {"label": "Alle markten", "url": _cycles_url(view, None), "on": not market}
        ]
        + [{"label": m, "url": _cycles_url(view, m), "on": m == market} for m in choices],
        "newer_url": _cycles_url(view, market, page - 1) if page > 1 else None,
        "older_url": _cycles_url(view, market, page + 1) if page < pages else None,
        "clear_url": _cycles_url("important", None),
    }


# -- open positions ----------------------------------------------------------------------------


def position_view(p: Position, price: float | None, fee_pct: float) -> dict[str, Any]:
    """An open position at ``price``: its value, the result after the costs of buying and
    selling (netto: the entry already includes the buy fee, the sell fee is taken off the
    value), and how far the price is from the stop and the take-profit."""
    value = p.amount * price if price and p.amount else None
    # Without what the buy cost there is no result to show (never the whole value as profit).
    pnl = (
        value * (1 - fee_pct / 100) - p.entry_quote if value is not None and p.entry_quote else None
    )
    pnl_pct = pnl / p.entry_quote * 100 if pnl is not None else None
    stop_gap = (price - p.stop_price) / price * 100 if price and p.stop_price else None
    tp_gap = (p.take_profit_price - price) / price * 100 if price and p.take_profit_price else None
    return {
        "p": p,
        "price": price,
        "value_eur": value,
        "pnl_eur": pnl,
        "pnl_pct": pnl_pct,
        "stop_gap_pct": stop_gap,
        "tp_gap_pct": tp_gap,
    }


# -- messages after an action ------------------------------------------------------------------

# The kill switch, in the words of the glossary (KILL_SWITCH_NL in app/web.py).
PAUSED_FLASH = "Noodstop aangezet: de bot koopt niets meer tot je hervat."
RESUMED_FLASH = "Kopen hervat: de bot mag weer kopen."


def _result_nl(r: Any) -> str:
    if r.source == "error" or r.signal == "error":
        return f"{r.market} mislukt"
    if r.action == "buy":
        return f"{r.market} gekocht" if r.executed else f"{r.market} kopen niet uitgevoerd"
    if r.action == "sell":
        return f"{r.market} verkocht" if r.executed else f"{r.market} verkopen niet uitgevoerd"
    return f"{r.market} afwachten"


CONSULT_NO_CLAUDE = (
    "Claude kan niet beoordelen: de Claude-sleutel ontbreekt (ANTHROPIC_API_KEY in de "
    "serveromgeving)."
)


def consult_flash(r: Any) -> tuple[str, str]:
    """(kind, text) after 'Claude laten beoordelen': 'Claude heeft BTC-EUR beoordeeld:
    afwachten. Zijn toelichting staat hieronder bij de markt.'"""
    if r.source == "error" or r.signal == "error":
        return "error", f"Beoordeling van {r.market} mislukt: {r.reason or 'onbekende fout'}."
    if r.source != "claude":
        # The rules decided before Claude could (risk-off, an order under way, no Claude).
        return "warn", (
            f"Claude is niet gevraagd voor {r.market}: {(r.reason or 'de regels besloten')}"
        ).rstrip(".") + "."
    what = _result_nl(r).removeprefix(f"{r.market} ")
    text = (
        f"Claude heeft {r.market} beoordeeld: {what}. Zijn toelichting staat hieronder bij de "
        "markt."
    )
    trouble = r.action in ("buy", "sell") and not r.executed
    return ("warn" if trouble else "ok"), text


def cycle_flash(results: list[Any]) -> tuple[str, str]:
    """(kind, text) after 'Nu een cyclus draaien': 'Cyclus gedraaid: BTC-EUR afwachten ·
    ETH-EUR gekocht.' A failed market or an order that did not go through makes it a warning."""
    if not results:
        return "warn", "Cyclus gedraaid, maar er zijn geen markten ingesteld."
    text = "Cyclus gedraaid: " + " · ".join(_result_nl(r) for r in results) + "."
    trouble = any(
        r.source == "error" or (r.action in ("buy", "sell") and not r.executed) for r in results
    )
    if trouble:
        text += " Waarom, zie je hieronder per markt."
    return ("warn" if trouble else "ok"), text
