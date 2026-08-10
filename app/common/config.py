"""Environment-backed application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_file: Path = Path("data/telegram.session")
    interest_smoothing_k: float = 100.0
    interest_reaction_weight: float = 3.0
    interest_comment_weight: float = 6.0
    interest_share_weight: float = 10.0
    interest_decay_lambda: float = 0.002
    rest_base_url: str = "http://rest-server:8000"
    request_timeout_seconds: float = 120.0
    telegram_channel_timeout_seconds: float = 12.0
    telegram_query_timeout_seconds: float = 75.0
    spotify_client_id: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    spotify_auth_bind_host: str = "127.0.0.1"
    spotify_token_file: Path = Path("data/spotify/refresh-token.json")
    spotify_api_base_url: str = "https://api.spotify.com/v1"
    spotify_accounts_base_url: str = "https://accounts.spotify.com"
    spotify_playlist_item_concurrency: int = 4
    lastfm_api_key: str = ""
    lastfm_api_base_url: str = "https://ws.audioscrobbler.com/2.0/"
    lastfm_cache_ttl_seconds: float = 3600.0
    lastfm_max_concurrency: int = 6
    lastfm_requests_per_second: float = 4.0


settings = Settings()
