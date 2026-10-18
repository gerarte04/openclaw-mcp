"""Small async client for Last.fm track similarity data."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.common.config import Settings


class LastfmApiError(RuntimeError):
    """Raised for a non-recoverable Last.fm response."""


class LastfmRateLimitError(LastfmApiError):
    """Raised when Last.fm asks the caller to slow down."""


class LastfmTemporaryError(LastfmApiError):
    """Raised for a temporary upstream failure."""


@dataclass(frozen=True)
class SimilarTrack:
    name: str
    artist: str
    similarity: float


class AsyncRateLimiter:
    """Space request starts so a shared public API budget is not exceeded."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1 / max(requests_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self.interval
        if delay:
            await asyncio.sleep(delay)


class LastfmClient:
    """Fetches and TTL-caches `track.getSimilar` responses."""

    def __init__(self, config: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client
        self._cache: dict[tuple[str, str], tuple[float, list[SimilarTrack]]] = {}
        self._similar_rate_limiter = AsyncRateLimiter(config.lastfm_requests_per_second)

    @property
    def similar_rate_limiter(self) -> AsyncRateLimiter:
        """Share one Last.fm request budget across playlist creations."""
        return self._similar_rate_limiter

    async def get_similar(self, artist: str, track: str) -> list[SimilarTrack]:
        if not self.config.lastfm_api_key:
            raise LastfmApiError("LASTFM_API_KEY is not configured")
        key = (artist.casefold(), track.casefold())
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        params = {
            "method": "track.getsimilar",
            "artist": artist,
            "track": track,
            "api_key": self.config.lastfm_api_key,
            "format": "json",
            "limit": 100,
            "autocorrect": 1,
        }
        try:
            if self.client:
                response = await self.client.get(self.config.lastfm_api_base_url, params=params)
            else:
                async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                    response = await client.get(self.config.lastfm_api_base_url, params=params)
        except httpx.TimeoutException as error:
            raise LastfmTemporaryError("Last.fm request timed out") from error
        except httpx.HTTPError as error:
            raise LastfmTemporaryError("Last.fm request failed") from error

        if response.status_code == 429:
            raise LastfmRateLimitError("Last.fm rate limit reached")
        if response.status_code >= 500:
            raise LastfmTemporaryError("Last.fm is temporarily unavailable")
        if response.is_error:
            raise LastfmApiError("Last.fm rejected the similarity request")
        try:
            payload = response.json()
        except ValueError as error:
            raise LastfmApiError("Last.fm returned invalid JSON") from error
        if "error" in payload:
            raise LastfmApiError("Last.fm could not find similar tracks")

        results: list[SimilarTrack] = []
        for item in payload.get("similartracks", {}).get("track", []):
            item_artist = item.get("artist", {}).get("name")
            item_name = item.get("name")
            try:
                similarity = float(item.get("match", 0))
            except (TypeError, ValueError):
                continue
            if isinstance(item_artist, str) and isinstance(item_name, str) and similarity > 0:
                results.append(SimilarTrack(item_name, item_artist, similarity))
        self._cache[key] = (time.monotonic() + self.config.lastfm_cache_ttl_seconds, results)
        return results


async def get_similar_limited(
    client: LastfmClient,
    seeds: list[tuple[str, str]],
    concurrency: int,
    requests_per_second: float = 4.0,
) -> list[list[SimilarTrack]]:
    """Fetch similarity lists within concurrency and public rate budgets."""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    limiter = getattr(client, "similar_rate_limiter", None) or AsyncRateLimiter(requests_per_second)

    async def request(seed: tuple[str, str]) -> list[SimilarTrack]:
        async with semaphore:
            await limiter.wait()
            return await client.get_similar(*seed)

    return await asyncio.gather(*(request(seed) for seed in seeds))
