import json
from pathlib import Path

import httpx
import pytest

from app.common.config import Settings
from rest_service.spotify_client import SpotifyClient
from rest_service.spotify_oauth import RefreshTokenStore


@pytest.mark.asyncio
async def test_spotify_client_refreshes_pkce_token_and_reads_top_tracks(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("test-refresh-token")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/token":
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": "test-access-token"})
        assert request.headers["Authorization"] == "Bearer test-access-token"
        assert request.url.path != "/v1/recommendations"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "top-id",
                        "name": "Top track",
                        "uri": "spotify:track:top-id",
                        "artists": [{"name": "Top artist"}],
                    }
                ]
            },
        )

    config = Settings(
        telegram_api_id=1,
        telegram_api_hash="test-hash",
        spotify_client_id="test-client-id",
        spotify_token_file=token_path,
        spotify_api_base_url="https://api.spotify.test/v1",
        spotify_accounts_base_url="https://accounts.spotify.test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        tracks = await SpotifyClient(config, http_client).get_top_tracks("short_term", 1)

    assert [track.id for track in tracks] == ["top-id"]
    assert requests == ["/api/token", "/v1/me/top/tracks"]


@pytest.mark.asyncio
async def test_spotify_client_creates_playlist_for_current_user(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("test-refresh-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "test-access-token"})
        assert request.method == "POST"
        assert request.url.path == "/v1/me/playlists"
        assert json.loads(request.content) == {
            "name": "Discoveries",
            "public": False,
            "description": "Created by Spotify discovery MCP.",
        }
        return httpx.Response(
            201,
            json={
                "id": "playlist-id",
                "external_urls": {"spotify": "https://open.spotify.com/playlist/playlist-id"},
            },
        )

    config = Settings(
        spotify_client_id="test-client-id",
        spotify_token_file=token_path,
        spotify_api_base_url="https://api.spotify.test/v1",
        spotify_accounts_base_url="https://accounts.spotify.test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        playlist = await SpotifyClient(config, http_client).create_playlist("Discoveries", False)

    assert playlist.id == "playlist-id"
