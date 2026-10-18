"""Telegram Bot API client for sending text and media messages."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.common.config import Settings
from app.common.models import (
    TelegramBotMessageRequest,
    TelegramBotMessageResponse,
    TelegramMedia,
    TelegramMediaType,
)

TELEGRAM_CAPTION_LIMIT = 1024

# sendMediaGroup accepts photos and videos together, but audio and documents
# only with their own kind. Animations cannot be part of an album at all.
ALBUM_KINDS = {
    TelegramMediaType.PHOTO: "visual",
    TelegramMediaType.VIDEO: "visual",
    TelegramMediaType.AUDIO: "audio",
    TelegramMediaType.DOCUMENT: "document",
}

SINGLE_MEDIA_METHODS = {
    TelegramMediaType.PHOTO: "sendPhoto",
    TelegramMediaType.VIDEO: "sendVideo",
    TelegramMediaType.ANIMATION: "sendAnimation",
    TelegramMediaType.AUDIO: "sendAudio",
    TelegramMediaType.DOCUMENT: "sendDocument",
}


class TelegramBotError(RuntimeError):
    """Base error; sent_message_ids lists messages delivered before the failure."""

    def __init__(self, message: str, sent_message_ids: list[int] | None = None) -> None:
        super().__init__(message)
        self.sent_message_ids = sent_message_ids or []


class TelegramBotNotConfiguredError(TelegramBotError):
    """Raised when the bot token or target chat is missing."""


class TelegramBotAuthError(TelegramBotError):
    """Raised when Telegram rejects the bot token."""


class TelegramBotRejectedError(TelegramBotError):
    """Raised when Telegram rejects the message itself (bad chat, media, markup)."""


class TelegramBotRateLimitError(TelegramBotError):
    """Raised when Telegram returns HTTP 429."""


class TelegramBotTemporaryError(TelegramBotError):
    """Raised when Telegram is unavailable or its connection failed."""


@dataclass(frozen=True)
class _Call:
    method: str
    payload: dict


def group_media(media: list[TelegramMedia]) -> list[list[TelegramMedia]]:
    """Split media into consecutive runs that Telegram can send as one album."""
    groups: list[list[TelegramMedia]] = []
    for item in media:
        kind = ALBUM_KINDS.get(item.type)
        previous = groups[-1] if groups else None
        if kind and previous and ALBUM_KINDS.get(previous[0].type) == kind:
            previous.append(item)
        else:
            groups.append([item])
    return groups


def plan_calls(request: TelegramBotMessageRequest, chat_id: str) -> list[_Call]:
    """Translate one logical message into ordered Bot API calls."""
    base: dict = {"chat_id": chat_id}
    text = request.text.strip()
    formatting = {"parse_mode": request.parse_mode.value} if request.parse_mode and text else {}

    calls: list[_Call] = []
    caption = ""
    if text and (not request.media or len(text) > TELEGRAM_CAPTION_LIMIT):
        calls.append(_Call("sendMessage", {**base, "text": text, **formatting}))
    elif text:
        caption = text

    for index, group in enumerate(group_media(request.media)):
        group_caption = {"caption": caption, **formatting} if caption and index == 0 else {}
        if len(group) == 1:
            item = group[0]
            calls.append(
                _Call(
                    SINGLE_MEDIA_METHODS[item.type],
                    {**base, item.type.value: item.url, **group_caption},
                )
            )
        else:
            album = [{"type": item.type.value, "media": item.url} for item in group]
            album[0].update(group_caption)
            calls.append(_Call("sendMediaGroup", {**base, "media": album}))
    return calls


class TelegramBotClient:
    """Sends messages through the Bot API using TELEGRAM_BOT_TOKEN."""

    def __init__(self, config: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client

    async def send_message(self, request: TelegramBotMessageRequest) -> TelegramBotMessageResponse:
        if not self.config.telegram_bot_token:
            raise TelegramBotNotConfiguredError("TELEGRAM_BOT_TOKEN is not configured")
        chat_id = request.chat_id or self.config.telegram_bot_default_chat_id.strip()
        if not chat_id:
            raise TelegramBotNotConfiguredError(
                "chat_id was not given and TELEGRAM_BOT_DEFAULT_CHAT_ID is not configured"
            )

        message_ids: list[int] = []
        for call in plan_calls(request, chat_id):
            try:
                result = await self._call(call)
            except TelegramBotError as error:
                error.sent_message_ids = list(message_ids)
                raise
            messages = result if isinstance(result, list) else [result]
            message_ids.extend(message["message_id"] for message in messages)
        return TelegramBotMessageResponse(chat_id=chat_id, message_ids=message_ids)

    async def _call(self, call: _Call) -> dict | list:
        # The URL embeds the bot token, so httpx error texts must never be surfaced.
        url = (
            f"{self.config.telegram_bot_api_base_url.rstrip('/')}"
            f"/bot{self.config.telegram_bot_token}/{call.method}"
        )
        try:
            if self.client:
                response = await self.client.post(url, json=call.payload)
            else:
                async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                    response = await client.post(url, json=call.payload)
        except httpx.TimeoutException:
            raise TelegramBotTemporaryError("Telegram Bot API request timed out") from None
        except httpx.HTTPError:
            raise TelegramBotTemporaryError("Telegram Bot API request failed") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        description = str(payload.get("description") or "Telegram Bot API request failed")

        if response.status_code in {401, 404}:
            raise TelegramBotAuthError("Telegram rejected the bot token")
        if response.status_code == 429:
            raise TelegramBotRateLimitError(description)
        if response.status_code >= 500:
            raise TelegramBotTemporaryError("Telegram Bot API is temporarily unavailable")
        if response.status_code in {400, 403}:
            raise TelegramBotRejectedError(description)
        if response.is_error or not payload.get("ok") or "result" not in payload:
            raise TelegramBotError(description)
        return payload["result"]
