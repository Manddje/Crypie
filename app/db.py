"""Database models and session handling (SQLAlchemy 2.x)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
    validates,
)

from app.config import get_settings

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def clip(value: str | None, length: int) -> str | None:
    """Cut free text to its column length, so a long message can never fail a commit."""
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= length else value[: max(length - 1, 0)] + "…"


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
    # pending (written before it is sent) | rejected (never placed) | unknown (sent, outcome
    # not known yet) | a Bitvavo status: filled, partiallyFilled, canceledMarketProtection...
    status: Mapped[str] = mapped_column(String(32))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why the bot traded
    fee_quote: Mapped[float | None] = mapped_column(Float, nullable=True)  # EUR fee paid
    # Our own id, sent as Bitvavo's clientOrderId, so a timed-out order can be looked up.
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # bot position

    @validates("status", "exchange_order_id")
    def _clip_text(self, key: str, value: str | None) -> str | None:
        return clip(value, self.__table__.c[key].type.length)


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
    # RSS only: the site of the feed the item came from. A feed chooses its own links, so the
    # feed (not the link host) is the outlet when independent sources are counted.
    feed_host: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

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
    # Failed Claude assessments; after a few the item is set aside instead of retried forever.
    assess_attempts: Mapped[int] = mapped_column(Integer, default=0)

    def coin_list(self) -> list[str]:
        return [c for c in (self.coins or "").split(",") if c]

    @property
    def safe_url(self) -> str | None:
        """The article link when it is plain http(s), else None (never a javascript: link)."""
        from urllib.parse import urlsplit

        url = (self.url or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if any(ord(ch) < 33 or ch == "\x7f" for ch in url):
            return None
        return url


ACTIVE_POSITION_STATUSES = ("open", "opening", "closing")


class Position(Base):
    """An open or closed long position in one market (paper or live).

    ``status`` is open | closed, or briefly ``opening`` (a buy is in flight) or ``closing``
    (a sell is in flight). Those two claim the market so no other run can trade it meanwhile;
    the database allows one such position per mode and market, so two processes (a rolling
    deploy) can never both open one.
    """

    __tablename__ = "positions"
    __table_args__ = (
        Index(
            "uq_active_position",
            "mode",
            "market",
            unique=True,
            sqlite_where=text("status IN ('open', 'opening', 'closing')"),
            postgresql_where=text("status IN ('open', 'opening', 'closing')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    market: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open|closed
    amount: Mapped[float] = mapped_column(Float)  # base currency held
    entry_price: Mapped[float] = mapped_column(Float)
    entry_quote: Mapped[float] = mapped_column(Float)  # EUR spent incl. fee
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # peak since entry
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
    # Trade journal: the owner's own notes on this trade, written on /journal.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(128), nullable=True)  # "fout,nieuws"
    note_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def tag_list(self) -> list[str]:
        return [t for t in (self.tags or "").split(",") if t]

    @validates("close_reason")
    def _clip_text(self, key: str, value: str | None) -> str | None:
        return clip(value, self.__table__.c[key].type.length)


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

    @validates("blocked_by", "news_label", "model")
    def _clip_text(self, key: str, value: str | None) -> str | None:
        return clip(value, self.__table__.c[key].type.length)

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
    # Random per account, so cookies of a deleted account never fit a new one ("" = not set yet,
    # filled in at startup).
    account_id: Mapped[str] = mapped_column(String(64), default="")
    # Bumped to log out every browser at once (password change, new authenticator, ...).
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    # One-time code printed by the CLI that allows linking an authenticator (HMAC, not plain).
    enrol_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enrol_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Wrong second-factor codes for this account counted just before the last successful
    # login; shown on /account so a leaked password plus code guessing does not go unnoticed.
    mfa_failures_before_login: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def mfa_enrolled(self) -> bool:
        return bool(self.totp_secret)


class OAuthClient(Base):
    """An app registered through OAuth dynamic client registration (RFC 7591), e.g. Claude's
    custom connector. Public clients only: no secret, PKCE always (app/auth/oauth.py)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    redirect_uris: Mapped[str] = mapped_column(Text)  # JSON list
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OAuthCode(Base):
    """A one-time authorization code, kept as its hash until it is exchanged or expires."""

    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(256), default="")
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthToken(Base):
    """An access or refresh token, kept as its hash. ``family`` ties the tokens of one grant
    together, so revoking (or a refresh) clears them all at once."""

    __tablename__ = "oauth_tokens"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # access | refresh
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    family: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthThrottle(Base):
    """Failed login attempts per client address or account, kept across restarts."""

    __tablename__ = "auth_throttle"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[float] = mapped_column(Float, default=0.0)  # epoch seconds
    last_failure: Mapped[float] = mapped_column(Float, default=0.0, index=True)  # epoch seconds


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
    widen_columns(engine)
    add_missing_indexes(engine)


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


def widen_columns(bind, metadata=None) -> list[str]:
    """Postgres only: grow VARCHAR columns whose model length is larger than in the database.

    SQLite does not enforce VARCHAR lengths, so there it is a no-op. Idempotent: a column that
    is already long enough is left alone. Returns the ``table.column`` names it widened.
    """
    from sqlalchemy import inspect

    if bind.dialect.name != "postgresql":
        return []
    metadata = metadata if metadata is not None else Base.metadata
    inspector = inspect(bind)
    widened: list[str] = []
    with bind.begin() as conn:
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            current = {c["name"]: c["type"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                want = getattr(column.type, "length", None)
                if not isinstance(column.type, String) or isinstance(column.type, Text) or not want:
                    continue
                have = getattr(current.get(column.name), "length", None)
                if have is None or have >= want:
                    continue  # missing, unbounded or already long enough
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ALTER COLUMN "{column.name}" '
                        f"TYPE VARCHAR({int(want)})"
                    )
                )
                widened.append(f"{table.name}.{column.name}")
    for name in widened:
        log.info("widened column %s", name)
    return widened


def add_missing_indexes(bind) -> None:
    """Create indexes that newer models define on tables that already existed.

    Never fails startup: an index that cannot be created (for example the unique index on
    active positions while old data still holds a duplicate) is logged and skipped.
    """
    from sqlalchemy import inspect

    inspector = inspect(bind)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in existing:
                continue
            try:
                index.create(bind)
            except Exception:  # noqa: BLE001 - log and keep starting
                log.exception(
                    "could not create index %s on %s; fix the data and restart",
                    index.name,
                    table.name,
                )


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
