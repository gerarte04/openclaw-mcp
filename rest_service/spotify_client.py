"""Spotify Web API client using a locally stored PKCE refresh token."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.common.config import Settings
from rest_service.spotify_oauth import RefreshTokenStore, SpotifyOAuthError


class SpotifyApiError(RuntimeError):
    """Raised for a non-retryable Spotify API response."""


class SpotifyAuthRequiredError(SpotifyApiError):
    """Raised when local authorization must be completed again."""


class SpotifyRateLimitError(SpotifyApiError):
    """Raised when Spotify returns HTTP 429."""


class SpotifyTemporaryError(SpotifyApiError):
    """Raised when Spotify is unavailable or its connection failed."""


@dataclass(frozen=True)
class SpotifyTrack:
    id: str
    name: str
    artist: str
    uri: str


@dataclass(frozen=True)
class SpotifyPlaylist:
    id: str
    url: str


class SpotifyClient:
    """Minimal Spotify API surface used by playlist discovery."""

    def __init__(self, config: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client
        self._access_token: str | None = None

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        for attempt in range(2):
            token = await self._access_token_or_raise(force_refresh=attempt == 1)
            try:
                if self.client:
                    response = await self.client.request(
                        method,
                        f"{self.config.spotify_api_base_url.rstrip('/')}/{path.lstrip('/')}",
                        headers={"Authorization": f"Bearer {token}"},
                        json=json,
                        params=params,
                    )
                else:
                    async with httpx.AsyncClient(
                        timeout=self.config.request_timeout_seconds
                    ) as client:
                        response = await client.request(
                            method,
                            f"{self.config.spotify_api_base_url.rstrip('/')}/{path.lstrip('/')}",
                            headers={"Authorization": f"Bearer {token}"},
                            json=json,
                            params=params,
                        )
            except httpx.TimeoutException as error:
                raise SpotifyTemporaryError("Spotify request timed out") from error
            except httpx.HTTPError as error:
                raise SpotifyTemporaryError("Spotify request failed") from error
            if response.status_code != 401 or attempt:
                return self._validate_response(response)
        raise SpotifyAuthRequiredError("Spotify access token was rejected")

    async def _access_token_or_raise(self, force_refresh: bool = False) -> str:
        if self._access_token and not force_refresh:
            return self._access_token
        try:
            refresh_token = RefreshTokenStore(self.config.spotify_token_file).read()
        except SpotifyOAuthError as error:
            raise SpotifyAuthRequiredError(str(error)) from error
        if not refresh_token or not self.config.spotify_client_id:
            raise SpotifyAuthRequiredError("Spotify authorization is required")
        try:
            if self.client:
                response = await self.client.post(
                    f"{self.config.spotify_accounts_base_url.rstrip('/')}/api/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.config.spotify_client_id,
                    },
                )
            else:
                async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                    response = await client.post(
                        f"{self.config.spotify_accounts_base_url.rstrip('/')}/api/token",
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                            "client_id": self.config.spotify_client_id,
                        },
                    )
        except httpx.HTTPError as error:
            raise SpotifyTemporaryError("Spotify token refresh failed") from error
        if response.status_code in {400, 401}:
            raise SpotifyAuthRequiredError("Spotify authorization has expired")
        response = self._validate_response(response)
        try:
            payload = response.json()
            access_token = payload["access_token"]
        except (KeyError, ValueError, TypeError) as error:
            raise SpotifyAuthRequiredError("Spotify token response is invalid") from error
        if not isinstance(access_token, str) or not access_token:
            raise SpotifyAuthRequiredError("Spotify token response has no access token")
        rotated_refresh_token = payload.get("refresh_token")
        if isinstance(rotated_refresh_token, str) and rotated_refresh_token:
            try:
                RefreshTokenStore(self.config.spotify_token_file).write(rotated_refresh_token)
            except SpotifyOAuthError as error:
                raise SpotifyAuthRequiredError("Spotify refresh token cannot be saved") from error
        self._access_token = access_token
        return access_token

    @staticmethod
    def _validate_response(response: httpx.Response) -> httpx.Response:
        if response.status_code == 429:
            raise SpotifyRateLimitError("Spotify rate limit reached")
        if response.status_code >= 500:
            raise SpotifyTemporaryError("Spotify is temporarily unavailable")
        if response.status_code == 401:
            raise SpotifyAuthRequiredError("Spotify access token was rejected")
        if response.is_error:
            raise SpotifyApiError(f"Spotify request failed with status {response.status_code}")
        return response

    async def get_top_tracks(self, time_range: str, limit: int) -> list[SpotifyTrack]:
        response = await self._request(
            "GET", "/me/top/tracks", params={"time_range": time_range, "limit": limit}
        )
        return self._parse_tracks(response.json().get("items", []))

    async def search_track(self, name: str, artist: str) -> list[SpotifyTrack]:
        response = await self._request(
            "GET",
            "/search",
            params={"q": f"track:{name} artist:{artist}", "type": "track", "limit": 5},
        )
        return self._parse_tracks(response.json().get("tracks", {}).get("items", []))

    async def create_playlist(self, name: str, public: bool) -> SpotifyPlaylist:
        response = await self._request(
            "POST",
            "/me/playlists",
            json={
                "name": name,
                "public": public,
                "description": "Created by Spotify discovery MCP.",
            },
        )
        payload = response.json()
        playlist_id = payload.get("id")
        url = payload.get("external_urls", {}).get("spotify")
        if not isinstance(playlist_id, str) or not isinstance(url, str):
            raise SpotifyApiError("Spotify playlist response is invalid")
        return SpotifyPlaylist(playlist_id, url)

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        for offset in range(0, len(uris), 100):
            await self._request(
                "POST",
                f"/playlists/{playlist_id}/items",
                json={"uris": uris[offset : offset + 100]},
            )

    @staticmethod
    def _parse_tracks(items: list[dict]) -> list[SpotifyTrack]:
        tracks: list[SpotifyTrack] = []
        for item in items:
            track_id = item.get("id")
            name = item.get("name")
            uri = item.get("uri")
            artists = item.get("artists") or []
            artist = artists[0].get("name") if artists else None
            if all(isinstance(value, str) and value for value in (track_id, name, artist, uri)):
                tracks.append(SpotifyTrack(track_id, name, artist, uri))
        return tracks
