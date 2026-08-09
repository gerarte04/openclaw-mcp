import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")

from mcp_server.server import fetch_top_news  # noqa: E402


@pytest.mark.asyncio
async def test_mcp_translates_auth_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            401,
            json={
                "detail": {
                    "error": "TELEGRAM_AUTH_REQUIRED",
                    "message": "Session is unavailable",
                }
            },
        )
    )

    with patch(
        "mcp_server.server.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await fetch_top_news(period="1d", included_channels=["news"])

    assert "TELEGRAM_AUTH_REQUIRED" in result


@pytest.mark.asyncio
async def test_mcp_returns_rest_payload() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"posts": [], "period": "1d", "scanned_channels": 1})
    )
    with patch(
        "mcp_server.server.httpx.AsyncClient",
        return_value=httpx.AsyncClient(transport=transport),
    ):
        result = await fetch_top_news(period="1d", included_channels=["news"])

    assert '"scanned_channels":1' in result.replace(" ", "")
