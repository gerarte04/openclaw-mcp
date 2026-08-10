"""Streamable HTTP MCP server that proxies getTopNews to the internal REST API."""

import json

import httpx
from mcp.server.fastmcp import FastMCP

from app.common.config import settings
from app.common.models import SpotifyDiscoveryPlaylistRequest, TopNewsRequest

mcp = FastMCP(
    "telegram-news",
    host="0.0.0.0",
    port=8001,
    streamable_http_path="/mcp",
)


def rest_error_payload(response: httpx.Response) -> dict[str, str]:
    """Convert either FastAPI's structured errors or malformed responses safely."""
    try:
        detail = response.json().get("detail", {})
    except (AttributeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return {
        "error": detail.get("error", "REST_ERROR"),
        "message": detail.get(
            "message", f"REST request failed with status {response.status_code}."
        ),
    }


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
        return json.dumps(rest_error_payload(response))
    return response.text


async def create_spotify_discovery_playlist(
    top_n: int = 35,
    playlist_name: str = "Discovery playlist",
    public: bool = False,
) -> str:
    """Create a Spotify playlist with unfamiliar tracks related to listening history.

    Spotify must have been authorized manually beforehand. The service uses the
    user's Spotify top tracks and Last.fm track similarities; it does not call
    Spotify's unavailable Recommendations API. Playlists are private by default.
    """
    try:
        request = SpotifyDiscoveryPlaylistRequest(
            top_n=top_n, playlist_name=playlist_name, public=public
        )
    except ValueError as error:
        return json.dumps({"error": "INVALID_REQUEST", "message": str(error)})

    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.post(
                f"{settings.rest_base_url.rstrip('/')}/spotify/discovery-playlist",
                json=request.model_dump(),
            )
    except httpx.HTTPError:
        return json.dumps(
            {
                "error": "REST_UNAVAILABLE",
                "message": "The Spotify discovery REST service is unavailable.",
            }
        )

    if response.status_code == 401:
        return json.dumps(
            {
                "error": "SPOTIFY_AUTH_REQUIRED",
                "message": (
                    "Spotify is not authorized. Tell the user to manually run "
                    "scripts/authorize_spotify.py; do not request secrets."
                ),
            }
        )
    if response.is_error:
        return json.dumps(rest_error_payload(response))
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


@mcp.tool(name="createSpotifyDiscoveryPlaylist")
async def create_spotify_discovery_playlist_tool(
    top_n: int = 35,
    playlist_name: str = "Discovery playlist",
    public: bool = False,
) -> str:
    """Create a private Spotify discovery playlist from top tracks and Last.fm."""
    return await create_spotify_discovery_playlist(top_n, playlist_name, public)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
