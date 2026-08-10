"""FastAPI entry point for ranked Telegram posts."""

from fastapi import FastAPI, HTTPException, status

from app.common.config import settings
from app.common.models import (
    ErrorResponse,
    SpotifyDiscoveryPlaylistRequest,
    SpotifyDiscoveryPlaylistResponse,
    TopNewsRequest,
    TopNewsResponse,
)
from rest_service.lastfm_client import (
    LastfmApiError,
    LastfmRateLimitError,
    LastfmTemporaryError,
    LastfmClient,
)
from rest_service.spotify_client import (
    SpotifyApiError,
    SpotifyAuthRequiredError,
    SpotifyRateLimitError,
    SpotifyTemporaryError,
    SpotifyClient,
)
from rest_service.spotify_discovery import DiscoveryPlaylistError, SpotifyDiscoveryService
from rest_service.telegram_client import (
    TelegramAuthRequiredError,
    TelegramNewsRepository,
    UnknownChannelSelectorError,
)

app = FastAPI(title="Telegram News and Spotify Discovery REST API", version="0.2.0")
repository = TelegramNewsRepository(settings)
spotify_discovery = SpotifyDiscoveryService(SpotifyClient(settings), LastfmClient(settings))


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
