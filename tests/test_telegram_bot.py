import json
import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

from app.common.config import Settings
from app.common.models import TelegramBotMessageRequest
from mcp_server.server import send_telegram_bot_message
from rest_service.telegram_bot import (
    TelegramBotChatNotAllowedError,
    TelegramBotClient,
    TelegramBotNotConfiguredError,
    TelegramBotRejectedError,
    plan_calls,
)


def make_config(**overrides) -> Settings:
    values = {
        "telegram_api_id": 1,
        "telegram_api_hash": "test-hash",
        "telegram_bot_token": "123:secret",
        "telegram_bot_default_chat_id": "42",
        "telegram_bot_api_base_url": "https://bot.telegram.test",
    }
    return Settings(**(values | overrides))


def photo(index: int) -> dict:
    return {"type": "photo", "url": f"https://example.test/{index}.jpg"}


def test_request_requires_text_or_media() -> None:
    with pytest.raises(ValueError):
        TelegramBotMessageRequest(text="  ")


def test_request_rejects_more_than_ten_media() -> None:
    with pytest.raises(ValueError):
        TelegramBotMessageRequest(media=[photo(index) for index in range(11)])


def test_request_rejects_non_http_media_url() -> None:
    with pytest.raises(ValueError):
        TelegramBotMessageRequest(media=[{"type": "photo", "url": "file:///etc/passwd"}])


def test_plan_sends_text_only_message() -> None:
    calls = plan_calls(TelegramBotMessageRequest(text="Hello", parse_mode="HTML"), "42")

    assert [(call.method, call.payload) for call in calls] == [
        ("sendMessage", {"chat_id": "42", "text": "Hello", "parse_mode": "HTML"})
    ]


def test_plan_uses_single_media_method_with_caption() -> None:
    request = TelegramBotMessageRequest(
        text="Look", media=[{"type": "video", "url": "https://example.test/a.mp4"}]
    )

    calls = plan_calls(request, "42")

    assert [(call.method, call.payload) for call in calls] == [
        ("sendVideo", {"chat_id": "42", "video": "https://example.test/a.mp4", "caption": "Look"})
    ]


def test_plan_groups_compatible_media_into_albums() -> None:
    request = TelegramBotMessageRequest(
        text="Caption",
        media=[
            photo(1),
            {"type": "video", "url": "https://example.test/2.mp4"},
            {"type": "animation", "url": "https://example.test/3.gif"},
            {"type": "document", "url": "https://example.test/4.pdf"},
            {"type": "document", "url": "https://example.test/5.pdf"},
        ],
    )

    calls = plan_calls(request, "42")

    assert [call.method for call in calls] == ["sendMediaGroup", "sendAnimation", "sendMediaGroup"]
    assert calls[0].payload["media"] == [
        {"type": "photo", "media": "https://example.test/1.jpg", "caption": "Caption"},
        {"type": "video", "media": "https://example.test/2.mp4"},
    ]
    assert "caption" not in calls[1].payload
    assert [item["type"] for item in calls[2].payload["media"]] == ["document", "document"]


def test_plan_sends_long_text_separately_from_media() -> None:
    text = "x" * 1025
    calls = plan_calls(TelegramBotMessageRequest(text=text, media=[photo(1)]), "42")

    assert [call.method for call in calls] == ["sendMessage", "sendPhoto"]
    assert calls[0].payload["text"] == text
    assert "caption" not in calls[1].payload


@pytest.mark.asyncio
async def test_client_sends_to_default_chat_and_collects_message_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sendMediaGroup"):
            return httpx.Response(
                200, json={"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

    request = TelegramBotMessageRequest(
        text="Hi",
        media=[photo(1), photo(2), {"type": "audio", "url": "https://example.test/a.mp3"}],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await TelegramBotClient(make_config(), http_client).send_message(request)

    assert result.chat_id == "42"
    assert result.message_ids == [1, 2, 3]
    assert [item.url.path for item in requests] == [
        "/bot123:secret/sendMediaGroup",
        "/bot123:secret/sendAudio",
    ]
    assert json.loads(requests[0].content)["chat_id"] == "42"


@pytest.mark.asyncio
async def test_client_prefers_explicit_chat_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["chat_id"] == "@channel"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await TelegramBotClient(make_config(), http_client).send_message(
            TelegramBotMessageRequest(text="Hi", chat_id="@channel")
        )

    assert result.chat_id == "@channel"


@pytest.mark.asyncio
async def test_client_restricts_chats_when_any_chat_is_disallowed() -> None:
    sent_to: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_to.append(json.loads(request.content)["chat_id"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    config = make_config(
        telegram_bot_allow_any_chat=False, telegram_bot_allowed_chat_ids="@Team, -1001"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramBotClient(config, http_client)
        for chat_id in (None, "42", "@team", "-1001"):
            await client.send_message(TelegramBotMessageRequest(text="Hi", chat_id=chat_id))
        with pytest.raises(TelegramBotChatNotAllowedError, match="@stranger"):
            await client.send_message(TelegramBotMessageRequest(text="Hi", chat_id="@stranger"))

    assert sent_to == ["42", "42", "@team", "-1001"]


def test_rest_endpoint_maps_disallowed_chat_to_403() -> None:
    from fastapi.testclient import TestClient

    from rest_service import main

    async def fail(request: TelegramBotMessageRequest):
        raise TelegramBotChatNotAllowedError("Chat @stranger is not allowed")

    with patch.object(main.telegram_bot, "send_message", fail):
        response = TestClient(main.app).post(
            "/telegram-bot/messages", json={"text": "Hi", "chat_id": "@stranger"}
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "TELEGRAM_BOT_CHAT_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_client_requires_token_and_chat() -> None:
    request = TelegramBotMessageRequest(text="Hi")

    with pytest.raises(TelegramBotNotConfiguredError):
        await TelegramBotClient(make_config(telegram_bot_token="")).send_message(request)
    with pytest.raises(TelegramBotNotConfiguredError):
        await TelegramBotClient(make_config(telegram_bot_default_chat_id="")).send_message(request)


@pytest.mark.asyncio
async def test_client_reports_partial_delivery_without_leaking_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
        return httpx.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "Bad Request: wrong file"},
        )

    request = TelegramBotMessageRequest(text="x" * 2000, media=[photo(1)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(TelegramBotRejectedError) as raised:
            await TelegramBotClient(make_config(), http_client).send_message(request)

    assert raised.value.sent_message_ids == [5]
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_client_hides_token_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(Exception) as raised:
            await TelegramBotClient(make_config(), http_client).send_message(
                TelegramBotMessageRequest(text="Hi")
            )

    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_mcp_forwards_media_to_rest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/telegram-bot/messages"
        assert json.loads(request.content)["media"] == [photo(1)]
        return httpx.Response(200, json={"chat_id": "42", "message_ids": [1]})

    with patch(
        "mcp_server.server.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ):
        result = await send_telegram_bot_message(text="Hi", media=[photo(1)])

    assert json.loads(result)["message_ids"] == [1]


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_media_type() -> None:
    result = await send_telegram_bot_message(
        media=[{"type": "sticker", "url": "https://example.test/a.webp"}]
    )

    assert "INVALID_REQUEST" in result


@pytest.mark.asyncio
async def test_mcp_translates_rest_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503,
            json={
                "detail": {
                    "error": "TELEGRAM_BOT_NOT_CONFIGURED",
                    "message": "TELEGRAM_BOT_TOKEN is not configured",
                }
            },
        )
    )
    with patch(
        "mcp_server.server.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await send_telegram_bot_message(text="Hi")

    assert "TELEGRAM_BOT_NOT_CONFIGURED" in result


def test_rest_endpoint_maps_partial_rejection() -> None:
    from fastapi.testclient import TestClient

    from rest_service import main

    async def fail(request: TelegramBotMessageRequest):
        raise TelegramBotRejectedError("Bad Request: chat not found", [9])

    with patch.object(main.telegram_bot, "send_message", fail):
        response = TestClient(main.app).post("/telegram-bot/messages", json={"text": "Hi"})

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "TELEGRAM_BOT_REJECTED",
        "message": "Bad Request: chat not found 1 Telegram message(s) had already been sent.",
    }
