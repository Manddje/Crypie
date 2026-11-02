"""Application settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.trading.events import default_calendar_lines


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # NaN or infinity would make every limit check pass silently (NaN compares False).
        allow_inf_nan=False,
        # Docker/Coolify secrets: a file /run/secrets/<setting name>, e.g. bitvavo_api_secret.
        secrets_dir="/run/secrets" if os.path.isdir("/run/secrets") else None,
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # A secret file wins over the env var: compose always passes every variable, as ""
        # when it is unset, which would otherwise hide the file.
        return init_settings, file_secret_settings, env_settings, dotenv_settings

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
        "https://cryptoslate.com/feed/",
        "https://www.newsbtc.com/feed/",
        "https://beincrypto.com/feed/",
        "https://www.dlnews.com/arc/outboundfeeds/rss/",
    ]
    cryptocv_enabled: bool = True  # free aggregator with per-coin archive, no key
    google_news_enabled: bool = True  # one Google News search feed per coin
    fear_greed_enabled: bool = True  # alternative.me index as market-wide context
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
    trend_context_enabled: bool = True  # PlanB-style 200-week MA + monthly RSI as Claude context
    # Risk-off: no new buys around scheduled macro moments and after a news shock.
    risk_off_enabled: bool = True
    risk_off_before_min: int = 60
    risk_off_after_min: int = 120
    shock_pause_hours: int = 6  # after highest-impact, strongly negative news; 0 = off
    event_calendar: Annotated[list[str], NoDecode] = default_calendar_lines()
    live_trading_ack: bool = False  # must be true to start in live mode
    # Exit loop: stop-loss, trailing stop and take-profit of every open position, this often.
    exit_check_interval_min: int = 2
    # Live: EUR the bot may manage. 0 = the whole Bitvavo account (exposure and the daily loss
    # limit are then computed over every balance on the account).
    live_capital_eur: float = 0.0
    max_trade_eur: float = 100.0  # per order
    max_exposure_pct: float = 50.0  # of total portfolio value in coins
    daily_loss_limit_pct: float = 5.0  # realized+unrealized loss today -> pause for the day
    max_trades_per_day: int = 6
    cooldown_min_after_stop: int = 240  # no new buy in a market after a stop-loss
    min_trade_eur: float = 10.0
    # Hard ceilings for the limits above. Only the environment sets them (they are not in the
    # portal), so a stolen session cannot raise the limits past what the server allows.
    max_trade_eur_ceiling: float = 1000.0
    max_exposure_pct_ceiling: float = 100.0
    max_trades_per_day_ceiling: int = 50
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
    # Web hardening. SETUP_TOKEN: secret for /setup while there is no account (empty = a new
    # one per start, printed in the log). TRUSTED_ORIGINS: extra origins (comma-separated, e.g.
    # "https://crypie.example.nl") allowed to POST besides the site itself.
    setup_token: str = ""
    trusted_origins: Annotated[list[str], NoDecode] = []
    api_docs_enabled: bool = False  # /docs, /redoc and /openapi.json (still behind the login)

    @field_validator("markets", mode="before")
    @classmethod
    def _split_markets(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = [m.strip().upper() for m in value.split(",") if m.strip()]
            # An empty env value (e.g. Coolify passing MARKETS="") means "use the default".
            return parsed or cls.model_fields["markets"].default
        return value

    @field_validator("exit_check_interval_min", mode="after")
    @classmethod
    def _clamp_exit_interval(cls, value: int) -> int:
        # Exits must never be switched off by a bad value; clamp instead of failing startup.
        return min(max(int(value), 1), 60)

    @field_validator("live_capital_eur", mode="after")
    @classmethod
    def _clamp_live_capital(cls, value: float) -> float:
        return value if value == value and 0 <= value < float("inf") else 0.0

    @field_validator("event_calendar", mode="before")
    @classmethod
    def _split_calendar(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = [line.strip() for line in re.split(r"[\r\n;]+", value) if line.strip()]
            return parsed or cls.model_fields["event_calendar"].default
        return value

    @field_validator("news_rss_feeds", mode="before")
    @classmethod
    def _split_feeds(cls, value: object) -> object:
        if isinstance(value, str):
            parsed = [f.strip() for f in value.split(",") if f.strip()]
            return parsed or cls.model_fields["news_rss_feeds"].default
        return value

    @field_validator("trusted_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip().rstrip("/").lower() for o in value.split(",") if o.strip()]
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
