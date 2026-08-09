"""FastAPI entry point for ranked Telegram posts."""

from fastapi import FastAPI, HTTPException, status

from app.common.config import settings
from app.common.models import ErrorResponse, TopNewsRequest, TopNewsResponse
from rest_service.telegram_client import (
    TelegramAuthRequiredError,
    TelegramNewsRepository,
    UnknownChannelSelectorError,
)

app = FastAPI(title="Telegram News REST API", version="0.1.0")
repository = TelegramNewsRepository(settings)


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
                "message": "Telegram is not authorized. Run scripts/authorize_telegram.py manually.",
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
