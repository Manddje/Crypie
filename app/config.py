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
    backtest_fee_pct: float = 0.25

    @field_validator("markets", mode="before")
    @classmethod
    def _split_markets(cls, value: object) -> object:
        if isinstance(value, str):
            return [m.strip().upper() for m in value.split(",") if m.strip()]
        return value

    @property
    def has_bitvavo_credentials(self) -> bool:
        return bool(self.bitvavo_api_key and self.bitvavo_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
