"""Database models and session handling (SQLAlchemy 2.x)."""

from __future__ import annotations

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

    def coin_list(self) -> list[str]:
        return [c for c in (self.coins or "").split(",") if c]


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


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
