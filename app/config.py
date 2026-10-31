"""Application settings, read from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    trading_mode: Literal["paper", "live"] = "paper"
    bitvavo_api_key: str = ""
    bitvavo_api_secret: str = ""
    bitvavo_rest_url: str = "https://api.bitvavo.com/v2"
    markets: Annotated[list[str], NoDecode] = ["BTC-EUR", "ETH-EUR"]
    database_url: str = "sqlite:///./data/crypie.sqlite3"
    paper_start_eur: float = 1000.0
    anthropic_api_key: str = ""
    # Model per task: rare interactive work gets Opus, high-volume work gets cheaper models.
    claude_model_strategy: str = "claude-opus-5-5"
    claude_model_decision: str = "claude-sonnet-5-5"
    claude_model_news: str = "claude-haiku-5-5"
    claude_model_evaluation: str = "claude-fable-5-1"
    claude_monthly_budget_usd: float = 100.0

    # News (step 3)
    news_enabled: bool = True
    news_fetch_interval_min: int = 15
    news_rss_feeds: Annotated[list[str], NoDecode] = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://www.theblock.co/rss.xml",
        "https://decrypt.co/feed",
    ]
    gdelt_enabled: bool = True
    gdelt_query: str = (
        '(bitcoin OR ethereum OR crypto OR stablecoin OR "central bank" OR "interest rate" '
        "OR tariff OR sanctions OR SEC) sourcelang:english"
    )
    cryptopanic_token: str = ""
    cryptopanic_plan: str = "growth"  # path segment: growth | growth_weekly | enterprise
    news_max_age_hours: int = 48
    news_batch_size: int = 15

    # Trading (step 4). Hard limits are enforced in code, whatever Claude says.
    trading_enabled: bool = True
    trade_interval_min: int = 60
    decision_history_days: int = 7  # how far back /trading lists the cycles
    watch_interval_min: int = 5  # dip watch: re-check an armed market this often (0 = off)
    watch_max_min: int = 180  # dip watch: give up on a dip after this long without a turn
    live_trading_ack: bool = False  # must be true to start in live mode
    max_trade_eur: float = 100.0  # per order
    max_exposure_pct: float = 50.0  # of total portfolio value in coins
    daily_loss_limit_pct: float = 5.0  # realized+unrealized loss today -> pause for the day
    max_trades_per_day: int = 6
    cooldown_min_after_stop: int = 240  # no new buy in a market after a stop-loss
    min_trade_eur: float = 10.0
    claude_unavailable_mode: Literal["hold", "indicators"] = "hold"
    evaluation_interval_hours: int = 168
    backtest_fee_pct: float = 0.25
    # Bitvavo taker fee per side (percent). Default is the entry tier; live mode reads the
    # real fee from the account at startup when it can.
    trading_fee_pct: float = 0.25

    # Login (dashboard). The account is created in the browser at /setup and lives in the DB.
    auth_enabled: bool = True
    session_secret: str = ""  # optional; empty = generated once and kept in the database
    auth_session_hours: int = 24

    @field_validator("markets", mode="before")
    @classmethod
    def _split_markets(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = [m.strip().upper() for m in value.split(",") if m.strip()]
            # An empty env value (e.g. Coolify passing MARKETS="") means "use the default".
            return parsed or cls.model_fields["markets"].default
        return value

    @field_validator("news_rss_feeds", mode="before")
    @classmethod
    def _split_feeds(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = [f.strip() for f in value.split(",") if f.strip()]
            return parsed or cls.model_fields["news_rss_feeds"].default
        return value

    @property
    def coins(self) -> list[str]:
        """Base currencies of the configured markets, e.g. ['BTC', 'ETH']."""
        return [m.split("-")[0] for m in self.markets]

    @property
    def has_bitvavo_credentials(self) -> bool:
        return bool(self.bitvavo_api_key and self.bitvavo_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
