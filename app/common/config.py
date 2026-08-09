"""Environment-backed application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_file: Path = Path("data/telegram.session")
    interest_smoothing_k: float = 100.0
    interest_decay_lambda: float = 0.05
    rest_base_url: str = "http://rest-server:8000"
    request_timeout_seconds: float = 120.0
    telegram_channel_timeout_seconds: float = 12.0
    telegram_query_timeout_seconds: float = 75.0


settings = Settings()
