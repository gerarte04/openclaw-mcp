import json
from pathlib import Path

import httpx
import pytest

from app.common.config import Settings
from rest_service.spotify_client import SpotifyClient, SpotifyPlaylistUnavailableError
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


@pytest.mark.asyncio
async def test_spotify_client_paginates_library_and_skips_removed_items(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("test-refresh-token")
    requests: list[tuple[str, str, str]] = []

    def spotify_track(track_id: str) -> dict[str, object]:
        return {
            "id": track_id,
            "name": track_id,
            "uri": f"spotify:track:{track_id}",
            "artists": [{"name": "Artist"}],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "test-access-token"})
        offset = request.url.params.get("offset", "0")
        limit = request.url.params.get("limit", "")
        requests.append((request.url.path, offset, limit))
        if request.url.path == "/v1/me/tracks":
            if offset == "0":
                return httpx.Response(
                    200,
                    json={"items": [{"track": spotify_track("liked-1")}], "next": "next-page"},
                )
            return httpx.Response(
                200, json={"items": [{"track": spotify_track("liked-2")}], "next": None}
            )
        if request.url.path == "/v1/me/playlists":
            return httpx.Response(
                200,
                json={"items": [{"id": "readable"}, {"id": "forbidden"}], "next": None},
            )
        if request.url.path == "/v1/playlists/readable/items":
            return httpx.Response(
                200,
                json={
                    "items": [{"track": spotify_track("playlist-1")}, {"track": None}],
                    "next": None,
                },
            )
        if request.url.path == "/v1/playlists/forbidden/items":
            return httpx.Response(403, json={"error": {"status": 403}})
        raise AssertionError(f"Unexpected request: {request.url}")

    config = Settings(
        spotify_client_id="test-client-id",
        spotify_token_file=token_path,
        spotify_api_base_url="https://api.spotify.test/v1",
        spotify_accounts_base_url="https://accounts.spotify.test",
        spotify_playlist_item_concurrency=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        library = await SpotifyClient(config, http_client).get_library()

    assert [track.id for track in library.tracks] == ["liked-1", "liked-2", "playlist-1"]
    assert library.unavailable_playlists == 1
    assert ("/v1/me/tracks", "1", "50") in requests
    assert ("/v1/playlists/readable/items", "0", "50") in requests


@pytest.mark.asyncio
async def test_spotify_client_retries_429_using_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("test-refresh-token")
    calls = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "test-access-token"})
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"items": [], "next": None})

    monkeypatch.setattr("rest_service.spotify_client.asyncio.sleep", sleep)
    config = Settings(
        spotify_client_id="test-client-id",
        spotify_token_file=token_path,
        spotify_api_base_url="https://api.spotify.test/v1",
        spotify_accounts_base_url="https://accounts.spotify.test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        assert await SpotifyClient(config, http_client).get_saved_tracks() == []

    assert calls == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_spotify_client_marks_forbidden_playlist_as_unavailable(tmp_path: Path) -> None:
    token_path = tmp_path / "refresh-token.json"
    RefreshTokenStore(token_path).write("test-refresh-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "test-access-token"})
        return httpx.Response(403, json={"error": {"status": 403}})

    config = Settings(
        spotify_client_id="test-client-id",
        spotify_token_file=token_path,
        spotify_api_base_url="https://api.spotify.test/v1",
        spotify_accounts_base_url="https://accounts.spotify.test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(SpotifyPlaylistUnavailableError):
            await SpotifyClient(config, http_client).get_playlist_tracks("followed-playlist")
