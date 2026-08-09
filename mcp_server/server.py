"""stdio MCP server that proxies getTopNews to the internal REST API."""

import json

import httpx
from mcp.server.fastmcp import FastMCP

from app.common.config import settings
from app.common.models import TopNewsRequest

mcp = FastMCP("telegram-news")


async def fetch_top_news(
    period: str,
    exclude_mode: bool = False,
    excluded_channels: list[str] | None = None,
    included_channels: list[str] | None = None,
) -> str:
    """Return up to 50 most interesting posts from the user's Telegram channels.

    period is a compact duration (for example 1d, 1w, 9h32m). With exclude_mode
    false, supply included_channels. With true, all channels except
    excluded_channels are searched. Channel selectors accept title, username,
    @username, Telegram ID, or -100-prefixed ID.
    """
    try:
        request = TopNewsRequest(
            period=period,
            exclude_mode=exclude_mode,
            excluded_channels=excluded_channels or [],
            included_channels=included_channels or [],
        )
    except ValueError as error:
        return json.dumps({"error": "INVALID_REQUEST", "message": str(error)})

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(
                f"{settings.rest_base_url.rstrip('/')}/top-news",
                json=request.model_dump(),
            )
    except httpx.HTTPError:
        return json.dumps(
            {
                "error": "REST_UNAVAILABLE",
                "message": "The Telegram news REST service is unavailable.",
            }
        )

    if response.status_code == 401:
        return json.dumps(
            {
                "error": "TELEGRAM_AUTH_REQUIRED",
                "message": (
                    "Telegram is not authorized. Tell the user to manually run "
                    "scripts/authorize_telegram.py; do not request their login code or password."
                ),
            }
        )
    if response.is_error:
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        return json.dumps(
            {
                "error": detail.get("error", "REST_ERROR"),
                "message": detail.get("message", f"REST request failed with status {response.status_code}."),
            }
        )
    return response.text


@mcp.tool(name="getTopNews")
async def get_top_news(
    period: str,
    exclude_mode: bool = False,
    excluded_channels: list[str] | None = None,
    included_channels: list[str] | None = None,
) -> str:
    """Return up to 50 most interesting posts from the user's Telegram channels.

    period is a compact duration (for example 1d, 1w, 9h32m). With exclude_mode
    false, supply included_channels. With true, all channels except
    excluded_channels are searched. Channel selectors accept title, username,
    @username, Telegram ID, or -100-prefixed ID.
    """
    return await fetch_top_news(period, exclude_mode, excluded_channels, included_channels)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
