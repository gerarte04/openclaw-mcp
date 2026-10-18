"""FastAPI entry point for ranked Telegram posts."""

from fastapi import FastAPI, HTTPException, status

from app.common.config import settings
from app.common.models import (
    ErrorResponse,
    SpotifyDiscoveryPlaylistRequest,
    SpotifyDiscoveryPlaylistResponse,
    TelegramBotMessageRequest,
    TelegramBotMessageResponse,
    TopNewsRequest,
    TopNewsResponse,
)
from rest_service.lastfm_client import (
    LastfmApiError,
    LastfmClient,
    LastfmRateLimitError,
    LastfmTemporaryError,
)
from rest_service.spotify_client import (
    SpotifyApiError,
    SpotifyAuthRequiredError,
    SpotifyClient,
    SpotifyRateLimitError,
    SpotifyTemporaryError,
)
from rest_service.spotify_discovery import DiscoveryPlaylistError, SpotifyDiscoveryService
from rest_service.telegram_bot import (
    TelegramBotAuthError,
    TelegramBotChatNotAllowedError,
    TelegramBotClient,
    TelegramBotError,
    TelegramBotNotConfiguredError,
    TelegramBotRateLimitError,
    TelegramBotRejectedError,
    TelegramBotTemporaryError,
)
from rest_service.telegram_client import (
    TelegramAuthRequiredError,
    TelegramNewsRepository,
    UnknownChannelSelectorError,
)

app = FastAPI(title="Telegram News and Spotify Discovery REST API", version="0.2.0")
repository = TelegramNewsRepository(settings)
spotify_discovery = SpotifyDiscoveryService(SpotifyClient(settings), LastfmClient(settings))
telegram_bot = TelegramBotClient(settings)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Container liveness endpoint; it does not expose session state."""
    return {"status": "ok"}


@app.post(
    "/top-news",
    response_model=TopNewsResponse,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def top_news(request: TopNewsRequest) -> TopNewsResponse:
    try:
        posts, ignored, scanned_channels = await repository.get_top_news(request)
    except TelegramAuthRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "TELEGRAM_AUTH_REQUIRED",
                "message": (
                    "Telegram is not authorized. Run scripts/authorize_telegram.py manually."
                ),
            },
        ) from error
    except UnknownChannelSelectorError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "UNKNOWN_CHANNEL", "message": str(error)},
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "TELEGRAM_API_ERROR", "message": "Unable to fetch Telegram posts."},
        ) from error

    return TopNewsResponse(
        posts=posts,
        period=request.period,
        scanned_channels=scanned_channels,
        ignored_channel_selectors=ignored,
    )


@app.post(
    "/spotify/discovery-playlist",
    response_model=SpotifyDiscoveryPlaylistResponse,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def create_spotify_discovery_playlist(
    request: SpotifyDiscoveryPlaylistRequest,
) -> SpotifyDiscoveryPlaylistResponse:
    """Create a Spotify playlist using top tracks and Last.fm similarities."""
    try:
        return await spotify_discovery.create(request)
    except SpotifyAuthRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "SPOTIFY_AUTH_REQUIRED",
                "message": "Spotify is not authorized. Run scripts/authorize_spotify.py manually.",
            },
        ) from error
    except (SpotifyRateLimitError, LastfmRateLimitError) as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "UPSTREAM_RATE_LIMIT",
                "message": "Spotify or Last.fm rate limit reached.",
            },
        ) from error
    except DiscoveryPlaylistError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "NO_PLAYLIST_TRACKS", "message": str(error)},
        ) from error
    except LastfmTemporaryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "LASTFM_TEMPORARY_ERROR",
                "message": "Last.fm is temporarily unavailable.",
            },
        ) from error
    except SpotifyTemporaryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "SPOTIFY_TEMPORARY_ERROR",
                "message": "Spotify is temporarily unavailable.",
            },
        ) from error
    except LastfmApiError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "LASTFM_API_ERROR",
                "message": "Last.fm could not provide recommendations.",
            },
        ) from error
    except SpotifyApiError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "SPOTIFY_API_ERROR",
                "message": "Spotify could not create the playlist.",
            },
        ) from error


def telegram_bot_error_message(message: str, error: TelegramBotError) -> str:
    """Warn callers about partial delivery so a retry does not duplicate messages."""
    if error.sent_message_ids:
        count = len(error.sent_message_ids)
        return f"{message} {count} Telegram message(s) had already been sent."
    return message


@app.post(
    "/telegram-bot/messages",
    response_model=TelegramBotMessageResponse,
    responses={
        403: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def send_telegram_bot_message(
    request: TelegramBotMessageRequest,
) -> TelegramBotMessageResponse:
    """Send text and optional media through the configured Telegram bot."""
    try:
        return await telegram_bot.send_message(request)
    except TelegramBotNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "TELEGRAM_BOT_NOT_CONFIGURED", "message": str(error)},
        ) from error
    except TelegramBotChatNotAllowedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "TELEGRAM_BOT_CHAT_NOT_ALLOWED", "message": str(error)},
        ) from error
    except TelegramBotAuthError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "TELEGRAM_BOT_AUTH_ERROR",
                "message": telegram_bot_error_message(
                    "Telegram rejected TELEGRAM_BOT_TOKEN.", error
                ),
            },
        ) from error
    except TelegramBotRejectedError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "TELEGRAM_BOT_REJECTED",
                "message": telegram_bot_error_message(str(error), error),
            },
        ) from error
    except TelegramBotRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "UPSTREAM_RATE_LIMIT",
                "message": telegram_bot_error_message(
                    "Telegram Bot API rate limit reached.", error
                ),
            },
        ) from error
    except TelegramBotTemporaryError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "TELEGRAM_BOT_TEMPORARY_ERROR",
                "message": telegram_bot_error_message(
                    "Telegram Bot API is temporarily unavailable.", error
                ),
            },
        ) from error
    except TelegramBotError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "TELEGRAM_BOT_API_ERROR",
                "message": telegram_bot_error_message(
                    "Telegram could not send the message.", error
                ),
            },
        ) from error
