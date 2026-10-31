"""Database models and session handling (SQLAlchemy 2.x)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Order(Base):
    """Every order the app placed, paper or live, with the exchange's response."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mode: Mapped[str] = mapped_column(String(8))  # paper | live
    market: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    order_type: Mapped[str] = mapped_column(String(16))  # market | limit
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # base currency
    amount_quote: Mapped[float | None] = mapped_column(Float, nullable=True)  # EUR
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # fill or limit price
    status: Mapped[str] = mapped_column(String(16))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why the bot traded
    fee_quote: Mapped[float | None] = mapped_column(Float, nullable=True)  # EUR fee paid


class Setting(Base):
    """Free-form key/value store for runtime configuration (indicators, limits, ...)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ClaudeUsage(Base):
    """One row per Claude API call, so spend can be capped per month."""

    __tablename__ = "claude_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32))  # strategy | news | decision
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class NewsItem(Base):
    """A news article from any source, plus Claude's assessment once scored."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # rss | gdelt | cryptopanic
    source_name: Mapped[str] = mapped_column(String(128))  # e.g. "CoinDesk"
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_coins: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "BTC,ETH"
    source_votes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON (CryptoPanic)

    # Assessment (filled by Claude)
    assessed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assessment_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)  # -1 .. 1
    impact: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 .. 3
    horizon: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hours|days|weeks
    coins: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "BTC,ETH" or "ALL"
    summary_nl: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Same news fact in several outlets -> same key, so the sentiment counts it once.
    event_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    def coin_list(self) -> list[str]:
        return [c for c in (self.coins or "").split(",") if c]


class Position(Base):
    """An open or closed long position in one market (paper or live)."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    market: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open|closed
    amount: Mapped[float] = mapped_column(Float)  # base currency held
    entry_price: Mapped[float] = mapped_column(Float)
    entry_quote: Mapped[float] = mapped_column(Float)  # EUR spent incl. fee
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_quote: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    open_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fees_eur: Mapped[float] = mapped_column(Float, default=0.0)  # entry + exit fees, EUR


class Decision(Base):
    """Journal: every trading-cycle decision with its inputs, reasoning and later outcome."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    mode: Mapped[str] = mapped_column(String(8))
    market: Mapped[str] = mapped_column(String(16), index=True)
    price: Mapped[float] = mapped_column(Float)
    signal: Mapped[str] = mapped_column(String(8))  # strategy signal
    indicators: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    checks: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: strategy conditions
    news_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    news_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_position: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16))  # claude | rules | risk | mechanical
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(8))  # buy | sell | hold
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_quote: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Outcome: price change after the decision, filled in later
    price_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_72h: Mapped[float | None] = mapped_column(Float, nullable=True)

    def check_list(self) -> dict[str, list[dict]]:
        """Parsed ``checks`` JSON: {"buy": [...], "sell": [...]} with text/left/right/ok."""
        if not self.checks:
            return {"buy": [], "sell": []}
        data = json.loads(self.checks)
        return {"buy": data.get("buy", []), "sell": data.get("sell", [])}

    def indicator_values(self) -> dict[str, float]:
        return json.loads(self.indicators) if self.indicators else {}


class Evaluation(Base):
    """Periodic self-review by Claude of the recent decisions and trades."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str] = mapped_column(String(64))
    period_days: Mapped[int] = mapped_column(Integer)
    report_nl: Mapped[str] = mapped_column(Text)
    proposed_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    proposal_backtest: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthUser(Base):
    """The dashboard account: password hash plus the state of the second factor."""

    __tablename__ = "auth_users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    totp_last_counter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recovery_codes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of hashes
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def mfa_enrolled(self) -> bool:
        return bool(self.totp_secret)


def make_engine(url: str | None = None):
    url = url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    import os

    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
    Base.metadata.create_all(engine)
    add_missing_columns(engine)


def add_missing_columns(bind) -> None:
    """Tiny forward-only migration: add columns that newer models define on existing tables."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=bind.dialect)
                default = ""
                if column.default is not None and column.default.is_scalar:
                    default = f" DEFAULT {column.default.arg!r}"
                conn.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}{default}")
                )


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
