"""Request and response models shared by REST and MCP services."""

from datetime import datetime
import re

from pydantic import BaseModel, Field, field_validator, model_validator

PERIOD_PATTERN = re.compile(
    r"^(?:(?P<weeks>\d+)w)?(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?$",
    re.IGNORECASE,
)


class TopNewsRequest(BaseModel):
    """Filters used to fetch and rank Telegram channel posts."""

    period: str = Field(
        description="Compact duration such as 1w, 1d, 9h32m, or 1w2d3h4m."
    )
    exclude_mode: bool = Field(
        default=False,
        description="When true, use all channels except excluded_channels.",
    )
    excluded_channels: list[str] = Field(default_factory=list)
    included_channels: list[str] = Field(default_factory=list)

    @field_validator("period")
    @classmethod
    def validate_period(cls, value: str) -> str:
        match = PERIOD_PATTERN.fullmatch(value.strip())
        if not match or not any(int(part or 0) for part in match.groupdict().values()):
            raise ValueError("period must be a non-zero compact duration, for example 1d or 9h32m")
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_channel_mode(self) -> "TopNewsRequest":
        if not self.exclude_mode and not self.included_channels:
            raise ValueError("included_channels must not be empty when exclude_mode is false")
        return self


class NewsPost(BaseModel):
    text: str
    url: str
    created_at: datetime
    interest_score: float
    channel_id: int
    channel_title: str
    message_id: int


class TopNewsResponse(BaseModel):
    posts: list[NewsPost]
    period: str
    scanned_channels: int
    ignored_channel_selectors: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: str
    message: str


class SpotifyDiscoveryPlaylistRequest(BaseModel):
    """Parameters for a private Spotify discovery playlist."""

    top_n: int = Field(
        default=35,
        ge=1,
        le=35,
        description="Number of top tracks used as the source pool (1 to 35).",
    )
    playlist_name: str = Field(
        default="Discovery playlist",
        min_length=1,
        max_length=100,
        description="Name for the Spotify playlist.",
    )
    public: bool = Field(
        default=False,
        description="Whether the playlist is public. Defaults to a private playlist.",
    )

    @field_validator("playlist_name")
    @classmethod
    def validate_playlist_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("playlist_name must not be empty")
        return value


class SpotifyDiscoveryPlaylistResponse(BaseModel):
    playlist_id: str
    playlist_url: str
    playlist_name: str
    public: bool
    source_tracks_added: int
    recommendations_added: int
    seed_counts: dict[str, int]
    recommendation_quotas: dict[str, int]
    warnings: list[str] = Field(default_factory=list)
