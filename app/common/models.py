"""Request and response models shared by REST and MCP services."""

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

PERIOD_PATTERN = re.compile(
    r"^(?:(?P<weeks>\d+)w)?(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?$",
    re.IGNORECASE,
)


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_MEDIA_LIMIT = 10


class TopNewsRequest(BaseModel):
    """Filters used to fetch and rank Telegram channel posts."""

    period: str = Field(description="Compact duration such as 1w, 1d, 9h32m, or 1w2d3h4m.")
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
    known_tracks_excluded: int
    unavailable_playlists: int
    warnings: list[str] = Field(default_factory=list)


class TelegramMediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    AUDIO = "audio"
    DOCUMENT = "document"


class TelegramMedia(BaseModel):
    """A media attachment that Telegram downloads from a public URL."""

    type: TelegramMediaType = Field(
        description="photo, video, animation (GIF), audio, or document."
    )
    url: str = Field(description="Public HTTP(S) URL that Telegram can download.")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^https?://\S+$", value, re.IGNORECASE):
            raise ValueError("media url must be an absolute http(s) URL")
        return value


class TelegramParseMode(StrEnum):
    HTML = "HTML"
    MARKDOWN_V2 = "MarkdownV2"


class TelegramBotMessageRequest(BaseModel):
    """A bot message with optional text and up to ten media attachments."""

    text: str = Field(
        default="",
        max_length=TELEGRAM_TEXT_LIMIT,
        description="Message text (up to 4096 characters). Used as the media caption when it fits.",
    )
    media: list[TelegramMedia] = Field(
        default_factory=list,
        max_length=TELEGRAM_MEDIA_LIMIT,
        description="Optional media attachments in display order (at most 10).",
    )
    chat_id: str | None = Field(
        default=None,
        description="Chat ID or @channel_username. Defaults to TELEGRAM_BOT_DEFAULT_CHAT_ID.",
    )
    parse_mode: TelegramParseMode | None = Field(
        default=None,
        description="Optional Telegram formatting mode for text: HTML or MarkdownV2.",
    )

    @field_validator("chat_id", mode="before")
    @classmethod
    def validate_chat_id(cls, value: object) -> object:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_content(self) -> "TelegramBotMessageRequest":
        if not self.text.strip() and not self.media:
            raise ValueError("a message needs text, media, or both")
        return self


class TelegramBotMessageResponse(BaseModel):
    chat_id: str
    message_ids: list[int]
