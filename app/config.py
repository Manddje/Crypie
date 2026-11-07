"""Application settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.trading.events import default_calendar_lines

log = logging.getLogger(__name__)

AUTH_SESSION_HOURS_MAX = 168  # a week
NEWS_MAX_AGE_HOURS_MAX = 720  # the portal's maximum, below the 30-day news retention
API_TOKEN_MIN_LENGTH = 32  # shorter tokens are ignored (app/auth/api_token.py)


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
    claude_model_shock: str = "claude-sonnet-5-5"
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
    # Daily net flows of the US spot ETFs as context for Claude. Without a key: bitcoin only,
    # from TFTC's open series. A free cryptoetf.today key (no card) adds ETH, SOL and XRP.
    etf_flows_enabled: bool = True
    cryptoetf_api_key: str = ""
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
    # ETF outflow as risk-off: no new buys in a coin while the net flows of its US spot
    # ETFs show strong outflow (strong), any outflow (any), or never (off).
    risk_off_etf_outflow: Literal["off", "strong", "any"] = "strong"
    # News shocks (app/trading/shock.py). claude: Claude tells a new shock from a repeat and
    # rechecks a running pause; fixed: a pause of SHOCK_PAUSE_HOURS without Claude. The fixed
    # pause is also the fallback when Claude is not available (no key, budget spent, an error).
    shock_mode: Literal["claude", "fixed"] = "claude"
    shock_min_hours: float = 1.0  # a Claude pause lasts at least this long
    shock_max_hours: float = 48.0  # and ends by itself after this
    shock_recheck_min: int = 60  # retry after a failed recheck (Claude picks 30-180 itself)
    shock_pause_hours: int = 2  # the fixed pause after a shock; 0 = off
    event_calendar: Annotated[list[str], NoDecode] = default_calendar_lines()
    live_trading_ack: bool = False  # must be true to start in live mode
    # Your statement that the live key can only trade (no withdraw) and has an IP whitelist in
    # Bitvavo. Not checked and not required: without it the dashboard reminds you in live mode.
    bitvavo_key_restricted: bool = False
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
    # API_TOKEN: a bearer token (at least 32 characters) that opens /api/* and /mcp without the
    # browser login, for Claude's MCP connection (app/mcp_server.py) and scripts. Empty = off.
    api_token: str = ""

    # Notifications outside the app (app/notify.py), environment only. NOTIFY_URL: an ntfy topic
    # (https://ntfy.sh/<secret topic>) or a webhook; https, a public host name, no user:password.
    # NOTIFY_TOKEN: optional bearer token. NOTIFY_FORMAT: auto | ntfy | json.
    # NOTIFY_EXIT_FAIL_MIN: report a failing stop-loss check after this many minutes.
    notify_url: str = ""
    notify_token: str = ""
    notify_format: Literal["auto", "ntfy", "json"] = "auto"
    notify_exit_fail_min: int = 10

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

    @field_validator("notify_url", mode="after")
    @classmethod
    def _check_notify_url(cls, value: str) -> str:
        # A bad address must not stop the app (and its exits); warn and leave it off. The URL
        # itself is a secret (whoever knows an ntfy topic can read it): never logged.
        value = (value or "").strip()
        if not value:
            return ""
        from app.notify import validate_notify_url

        try:
            return validate_notify_url(value)
        except ValueError as exc:
            log.warning("NOTIFY_URL is ignored: %s", exc)
            return ""

    @field_validator("notify_exit_fail_min", mode="after")
    @classmethod
    def _clamp_notify_exit_fail(cls, value: int) -> int:
        return min(max(int(value), 1), 1440)

    @field_validator("api_token", mode="after")
    @classmethod
    def _check_api_token(cls, value: str) -> str:
        # A short token would be guessable; better no token (and a warning) than a weak one.
        # The token itself is a secret: never logged.
        value = (value or "").strip()
        if value and len(value) < API_TOKEN_MIN_LENGTH:
            log.warning(
                "API_TOKEN is ignored: it has fewer than %d characters", API_TOKEN_MIN_LENGTH
            )
            return ""
        return value

    @field_validator("auth_session_hours", mode="after")
    @classmethod
    def _clamp_session_hours(cls, value: int) -> int:
        # A typo must neither make sessions live for years nor make login impossible; clamp
        # with a warning instead of failing startup.
        clamped = min(max(int(value), 1), AUTH_SESSION_HOURS_MAX)
        if clamped != value:
            log.warning("AUTH_SESSION_HOURS=%r is out of range; using %d", value, clamped)
        return clamped

    @field_validator("news_max_age_hours", mode="after")
    @classmethod
    def _clamp_news_age(cls, value: int) -> int:
        # Older articles than the news retention would be pruned and fetched again each run.
        clamped = min(max(int(value), 1), NEWS_MAX_AGE_HOURS_MAX)
        if clamped != value:
            log.warning("NEWS_MAX_AGE_HOURS=%r is out of range; using %d", value, clamped)
        return clamped

    @field_validator(
        "max_trade_eur_ceiling", "max_exposure_pct_ceiling", "max_trades_per_day_ceiling"
    )
    @classmethod
    def _positive_ceiling(cls, value: float, info) -> float:
        # A ceiling of 0 or below would clamp every limit to nothing (and below min_trade_eur);
        # fall back to the built-in ceiling with a warning instead of failing startup.
        if value > 0:
            return value
        default = cls.model_fields[info.field_name].default
        log.warning("%s=%r must be above 0; using %r", info.field_name.upper(), value, default)
        return default

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
