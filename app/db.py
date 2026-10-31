"""Database models and session handling (SQLAlchemy 2.x)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, String, Text, create_engine
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
