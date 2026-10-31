"""The trade journal: every position with the decisions behind it and the owner's notes.

A journal entry is one round trip (or an open position): the entry order and the decision
that led to it, the exit order and its decision, the net result after fees, and the note
the owner wrote afterwards. Nothing is duplicated: positions, orders and decisions are the
tables the trading engine already writes; this module only joins them and keeps the notes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Decision, Order, Position

TAG_RE = re.compile(r"[^a-z0-9àâäéèêëíìîïóòôöúùûüç_\-]+")
NOTE_MAX = 4000
TAGS_MAX = 10

# close_reason -> what the journal calls it
EXIT_NL: dict[str, str] = {
    "stop_loss": "stop-loss geraakt",
    "take_profit": "take-profit geraakt",
    "signal": "verkoopsignaal van de strategie",
    "news_exit": "verkocht op negatief nieuws",
    "reset": "oefenportefeuille gereset",
}


@dataclass
class JournalEntry:
    position: Position
    entry_decision: Decision | None = None
    exit_decision: Decision | None = None
    entry_order: Order | None = None
    exit_order: Order | None = None

    @property
    def id(self) -> int:
        return self.position.id

    @property
    def is_open(self) -> bool:
        return self.position.status == "open"

    @property
    def duration(self) -> timedelta | None:
        p = self.position
        end = p.closed_at if p.status == "closed" else datetime.now(UTC)
        if end is None or p.opened_at is None:
            return None
        start = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=UTC)
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        return end - start

    @property
    def duration_nl(self) -> str:
        d = self.duration
        if d is None:
            return "–"
        minutes = int(d.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        if hours < 48:
            return f"{hours} uur"
        return f"{hours // 24} dagen"

    @property
    def result(self) -> str:
        """win | loss | flat | open"""
        if self.is_open:
            return "open"
        pnl = self.position.pnl_eur or 0.0
        return "win" if pnl > 0 else "loss" if pnl < 0 else "flat"

    @property
    def exit_nl(self) -> str | None:
        reason = self.position.close_reason
        if reason is None:
            return None
        return EXIT_NL.get(reason, reason)

    @property
    def entry_source(self) -> str | None:
        return self.entry_decision.source if self.entry_decision else None

    @property
    def entry_reasoning(self) -> str | None:
        if self.entry_decision is not None and self.entry_decision.reasoning:
            return self.entry_decision.reasoning
        return self.entry_order.reason if self.entry_order else None

    @property
    def exit_reasoning(self) -> str | None:
        if self.exit_decision is not None and self.exit_decision.reasoning:
            return self.exit_decision.reasoning
        return self.exit_order.reason if self.exit_order else None

    @property
    def entry_news(self) -> str | None:
        return self.entry_decision.news_label if self.entry_decision else None

    @property
    def soft_filters_failed(self) -> list[str]:
        if self.entry_decision is None:
            return []
        return [
            c["text"]
            for c in self.entry_decision.check_list()["buy"]
            if c.get("soft") and not c.get("ok")
        ]

    def as_dict(self) -> dict[str, Any]:
        p = self.position
        return {
            "id": p.id,
            "mode": p.mode,
            "market": p.market,
            "status": p.status,
            "result": self.result,
            "opened_at": p.opened_at,
            "closed_at": p.closed_at,
            "duration_min": int(self.duration.total_seconds() // 60) if self.duration else None,
            "amount": p.amount,
            "entry_price": p.entry_price,
            "entry_quote": p.entry_quote,
            "exit_price": p.exit_price,
            "exit_quote": p.exit_quote,
            "fees_eur": p.fees_eur,
            "pnl_eur": p.pnl_eur,
            "pnl_pct": p.pnl_pct,
            "stop_price": p.stop_price,
            "take_profit_price": p.take_profit_price,
            "close_reason": p.close_reason,
            "entry_source": self.entry_source,
            "entry_model": self.entry_decision.model if self.entry_decision else None,
            "entry_confidence": self.entry_decision.confidence if self.entry_decision else None,
            "entry_news": self.entry_news,
            "entry_reasoning": self.entry_reasoning,
            "exit_reasoning": self.exit_reasoning,
            "note": p.note,
            "tags": p.tag_list(),
            "note_updated_at": p.note_updated_at,
        }


@dataclass
class JournalFilter:
    market: str | None = None
    status: str | None = None  # open | closed
    result: str | None = None  # win | loss
    reason: str | None = None  # close_reason
    tag: str | None = None
    days: int | None = None
    query: str | None = None  # free text in notes and reasoning

    @property
    def active(self) -> bool:
        return any(v not in (None, "") for v in self.__dict__.values())


def _attach(session: Session, positions: list[Position]) -> list[JournalEntry]:
    order_ids = {p.open_order_id for p in positions} | {p.close_order_id for p in positions}
    order_ids.discard(None)
    orders: dict[int, Order] = {}
    decisions: dict[int, Decision] = {}
    if order_ids:
        for o in session.scalars(select(Order).where(Order.id.in_(order_ids))):
            orders[o.id] = o
        for d in session.scalars(select(Decision).where(Decision.order_id.in_(order_ids))):
            decisions[d.order_id] = d
    return [
        JournalEntry(
            position=p,
            entry_decision=decisions.get(p.open_order_id),
            exit_decision=decisions.get(p.close_order_id),
            entry_order=orders.get(p.open_order_id),
            exit_order=orders.get(p.close_order_id),
        )
        for p in positions
    ]


def journal_entries(
    session: Session, mode: str, flt: JournalFilter | None = None, limit: int = 500
) -> list[JournalEntry]:
    """Open positions first, then closed ones newest first, after the filter."""
    flt = flt or JournalFilter()
    stmt = select(Position).where(Position.mode == mode)
    if flt.market:
        stmt = stmt.where(Position.market == flt.market)
    if flt.status in ("open", "closed"):
        stmt = stmt.where(Position.status == flt.status)
    if flt.reason:
        stmt = stmt.where(Position.close_reason == flt.reason)
    if flt.result == "win":
        stmt = stmt.where(Position.status == "closed", Position.pnl_eur > 0)
    elif flt.result == "loss":
        stmt = stmt.where(Position.status == "closed", Position.pnl_eur < 0)
    if flt.days:
        stmt = stmt.where(Position.opened_at >= datetime.now(UTC) - timedelta(days=flt.days))
    if flt.tag:
        stmt = stmt.where(Position.tags.like(f"%{normalize_tags(flt.tag)}%"))
    rows = list(session.scalars(stmt.order_by(Position.opened_at.desc()).limit(limit)))

    def _ts(p: Position) -> float:
        t = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=UTC)
        return t.timestamp()

    rows.sort(key=lambda p: (p.status != "open", -_ts(p), -(p.id or 0)))
    entries = _attach(session, rows)
    if flt.query:
        q = flt.query.lower()
        entries = [
            e
            for e in entries
            if q in (e.position.note or "").lower()
            or q in (e.entry_reasoning or "").lower()
            or q in (e.exit_reasoning or "").lower()
        ]
    return entries


def journal_entry(session: Session, position_id: int) -> JournalEntry | None:
    p = session.get(Position, position_id)
    if p is None:
        return None
    return _attach(session, [p])[0]


def journal_totals(entries: list[JournalEntry]) -> dict[str, Any]:
    closed = [e for e in entries if not e.is_open]
    wins = [e for e in closed if e.result == "win"]
    losses = [e for e in closed if e.result == "loss"]
    pnl = [e.position.pnl_eur or 0.0 for e in closed]
    durations = [e.duration for e in closed if e.duration is not None]
    avg_min = (
        int(sum(d.total_seconds() for d in durations) / len(durations) // 60) if durations else None
    )
    gross_win = sum(e.position.pnl_eur or 0 for e in wins)
    gross_loss = -sum(e.position.pnl_eur or 0 for e in losses)
    per_market: dict[str, dict[str, Any]] = {}
    for e in closed:
        m = per_market.setdefault(
            e.position.market, {"market": e.position.market, "trades": 0, "wins": 0, "pnl_eur": 0.0}
        )
        m["trades"] += 1
        m["wins"] += 1 if e.result == "win" else 0
        m["pnl_eur"] += e.position.pnl_eur or 0.0
    for m in per_market.values():
        m["pnl_eur"] = round(m["pnl_eur"], 2)
        m["win_rate_pct"] = round(m["wins"] / m["trades"] * 100, 1) if m["trades"] else None
    exits: dict[str, int] = {}
    for e in closed:
        exits[e.position.close_reason or "unknown"] = (
            exits.get(e.position.close_reason or "unknown", 0) + 1
        )
    return {
        "entries": len(entries),
        "open": len(entries) - len(closed),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "total_pnl_eur": round(sum(pnl), 2),
        "total_fees_eur": round(sum(e.position.fees_eur or 0 for e in closed), 2),
        "invested_eur": round(sum(e.position.entry_quote for e in closed), 2),
        "return_pct": round(sum(pnl) / sum(e.position.entry_quote for e in closed) * 100, 2)
        if closed and sum(e.position.entry_quote for e in closed) > 0
        else None,
        "avg_pnl_eur": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "avg_pnl_pct": round(sum(e.position.pnl_pct or 0 for e in closed) / len(closed), 2)
        if closed
        else None,
        "avg_win_eur": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_eur": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best_pct": round(max(e.position.pnl_pct or 0 for e in closed), 2) if closed else None,
        "worst_pct": round(min(e.position.pnl_pct or 0 for e in closed), 2) if closed else None,
        "avg_duration_min": avg_min,
        "with_note": sum(1 for e in entries if (e.position.note or "").strip()),
        "per_market": sorted(per_market.values(), key=lambda m: m["market"]),
        "exits": exits,
    }


def normalize_tags(raw: str | None) -> str:
    """'Fout, Nieuws FOMO' -> 'fout,nieuws-fomo'; at most TAGS_MAX unique tags."""
    if not raw:
        return ""
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        tag = TAG_RE.sub("-", part.strip().lower()).strip("-")
        if tag and tag not in out:
            out.append(tag)
    return ",".join(out[:TAGS_MAX])


def save_note(session: Session, position_id: int, note: str | None, tags: str | None) -> Position:
    p = session.get(Position, position_id)
    if p is None:
        raise LookupError(f"positie {position_id} bestaat niet")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"een notitie mag maximaal {NOTE_MAX} tekens lang zijn")
    p.note = note or None
    p.tags = normalize_tags(tags) or None
    p.note_updated_at = datetime.now(UTC) if (p.note or p.tags) else None
    session.commit()
    return p


def all_tags(session: Session, mode: str) -> list[str]:
    rows = session.scalars(
        select(Position.tags).where(Position.mode == mode, Position.tags.is_not(None))
    ).all()
    tags: set[str] = set()
    for r in rows:
        tags.update(t for t in (r or "").split(",") if t)
    return sorted(tags)


def notes_for_claude(entries: list[JournalEntry], limit: int = 20) -> list[dict[str, Any]]:
    """The owner's notes on recent trades, for the evaluator: what a human saw that the data
    does not show."""
    out = []
    for e in entries:
        if not (e.position.note or "").strip() and not e.position.tags:
            continue
        out.append(
            {
                "market": e.position.market,
                "opened_at": e.position.opened_at,
                "pnl_pct": round(e.position.pnl_pct, 2) if e.position.pnl_pct is not None else None,
                "exit": e.position.close_reason,
                "tags": e.position.tag_list(),
                "note": (e.position.note or "").strip() or None,
            }
        )
        if len(out) >= limit:
            break
    return out


def unlinked_orders(session: Session, mode: str, limit: int = 50) -> list[Order]:
    """Filled orders that belong to no position: manual orders from the dashboard or the API."""
    linked = set()
    for open_id, close_id in session.execute(
        select(Position.open_order_id, Position.close_order_id).where(Position.mode == mode)
    ):
        linked.update(x for x in (open_id, close_id) if x is not None)
    stmt = select(Order).where(Order.mode == mode).order_by(Order.id.desc()).limit(limit * 2)
    return [o for o in session.scalars(stmt) if o.id not in linked][:limit]
