"""Discovery-playlist selection algorithm backed by Spotify and Last.fm."""

from __future__ import annotations

import asyncio
import random
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil
from typing import Protocol

from app.common.models import (
    SpotifyDiscoveryPlaylistRequest,
    SpotifyDiscoveryPlaylistResponse,
)
from rest_service.lastfm_client import LastfmClient, SimilarTrack, get_similar_limited
from rest_service.spotify_client import SpotifyClient, SpotifyLibrary, SpotifyTrack


class DiscoveryPlaylistError(RuntimeError):
    """Raised when no useful playlist can be built from the available catalogue."""


class SpotifyProtocol(Protocol):
    async def get_top_tracks(self, time_range: str, limit: int) -> list[SpotifyTrack]: ...

    async def get_library(self) -> SpotifyLibrary: ...

    async def search_track(self, name: str, artist: str) -> list[SpotifyTrack]: ...

    async def create_playlist(self, name: str, public: bool): ...

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> None: ...


class LastfmProtocol(Protocol):
    config: object

    async def get_similar(self, artist: str, track: str) -> list[SimilarTrack]: ...


@dataclass(frozen=True)
class AggregatedCandidate:
    name: str
    artist: str
    score: float


SEGMENTS = ("short_term", "medium_term", "long_term")


def normalise(value: str) -> str:
    """Compare Spotify and Last.fm labels without punctuation/case differences."""
    return re.sub(r"\W+", "", value, flags=re.UNICODE).casefold()


def track_key(name: str, artist: str) -> tuple[str, str]:
    return normalise(name), normalise(artist)


@dataclass
class LibraryExclusions:
    """Request-scoped identity set for tracks already available to the user."""

    track_ids: set[str]
    track_keys: set[tuple[str, str]]
    excluded_candidate_keys: set[tuple[str, str]]
    excluded_spotify_ids: set[str]

    @classmethod
    def from_tracks(cls, tracks: Iterable[SpotifyTrack]) -> LibraryExclusions:
        values = list(tracks)
        return cls(
            track_ids={track.id for track in values},
            track_keys={track_key(track.name, track.artist) for track in values},
            excluded_candidate_keys=set(),
            excluded_spotify_ids=set(),
        )

    def exclude_candidate(self, key: tuple[str, str]) -> None:
        self.excluded_candidate_keys.add(key)

    def exclude_spotify_track(self, track: SpotifyTrack) -> None:
        self.excluded_spotify_ids.add(track.id)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_candidate_keys) + len(self.excluded_spotify_ids)


def select_seed_tracks(
    short_tracks: list[SpotifyTrack],
    medium_tracks: list[SpotifyTrack],
    long_tracks: list[SpotifyTrack],
    rng: random.Random,
) -> dict[str, list[SpotifyTrack]]:
    """Sample distinct seeds while preserving temporal diversity."""

    def sample(pool: list[SpotifyTrack], limit: int | None = None) -> list[SpotifyTrack]:
        count = ceil(len(pool) / 2)
        if limit is not None:
            count = min(count, limit)
        return rng.sample(pool, count) if count else []

    short_ids = {track.id for track in short_tracks}
    medium_pool = [track for track in medium_tracks if track.id not in short_ids]
    medium_ids = {track.id for track in medium_tracks}
    long_pool = [
        track for track in long_tracks if track.id not in short_ids and track.id not in medium_ids
    ]
    return {
        "short_term": sample(short_tracks),
        "medium_term": sample(medium_pool, limit=7),
        "long_term": sample(long_pool, limit=3),
    }


def proportional_quotas(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Allocate integer quotas with largest remainders and deterministic ties."""
    quotas = {segment: 0 for segment in SEGMENTS}
    weight_total = sum(weights.values())
    if total <= 0 or weight_total <= 0:
        return quotas
    remainders: list[tuple[float, str]] = []
    allocated = 0
    for segment in SEGMENTS:
        exact = total * weights[segment] / weight_total
        quotas[segment] = int(exact)
        allocated += quotas[segment]
        remainders.append((exact - quotas[segment], segment))
    for _, segment in sorted(remainders, key=lambda item: (-item[0], SEGMENTS.index(item[1])))[
        : total - allocated
    ]:
        quotas[segment] += 1
    return quotas


class SpotifyDiscoveryService:
    """Creates a Spotify playlist from diverse top tracks and Last.fm similarities."""

    def __init__(
        self,
        spotify: SpotifyClient,
        lastfm: LastfmClient,
        rng: random.Random | None = None,
    ) -> None:
        self.spotify = spotify
        self.lastfm = lastfm
        self.rng = rng or random.Random()

    async def create(
        self, request: SpotifyDiscoveryPlaylistRequest
    ) -> SpotifyDiscoveryPlaylistResponse:
        limits = {
            "short_term": request.top_n,
            "medium_term": ceil(request.top_n * 1.3),
            "long_term": ceil(request.top_n * 1.42),
        }
        short_tracks, medium_tracks, long_tracks, library = await asyncio.gather(
            self.spotify.get_top_tracks("short_term", limits["short_term"]),
            self.spotify.get_top_tracks("medium_term", limits["medium_term"]),
            self.spotify.get_top_tracks("long_term", limits["long_term"]),
            self.spotify.get_library(),
        )
        received = {
            "short_term": short_tracks,
            "medium_term": medium_tracks,
            "long_term": long_tracks,
        }
        seeds = select_seed_tracks(short_tracks, medium_tracks, long_tracks, self.rng)
        seed_counts = {segment: len(seeds[segment]) for segment in SEGMENTS}
        known_tracks = self._unique_tracks(
            track for segment in SEGMENTS for track in received[segment]
        )
        source_keys = {track_key(track.name, track.artist) for track in known_tracks}
        exclusions = LibraryExclusions.from_tracks(library.tracks)
        candidates = await self._similar_candidates(seeds, source_keys, exclusions)

        source_limit = min(10, ceil(request.top_n * 0.25))
        source_tracks = self.rng.sample(known_tracks, min(source_limit, len(known_tracks)))
        source_uris = self._unique_uris(source_tracks)
        recommendation_target = source_limit * 3
        quotas = proportional_quotas(recommendation_target, seed_counts)
        recommendation_tracks, actual_quotas, warnings = await self._resolve_recommendations(
            candidates, quotas, source_keys, exclusions, reserved_uris=set(source_uris)
        )
        uris = [*source_uris, *(track.uri for track in recommendation_tracks)]
        if not uris:
            raise DiscoveryPlaylistError(
                "No source tracks or Spotify-matched Last.fm recommendations are available"
            )
        if len(source_uris) < source_limit:
            warnings.append(
                f"Only {len(source_uris)} source tracks were available; "
                f"requested up to {source_limit}."
            )
        if len(recommendation_tracks) < recommendation_target:
            warnings.append(
                f"Only {len(recommendation_tracks)} new recommendations were available; "
                f"target was {recommendation_target}."
            )
        if library.unavailable_playlists:
            warnings.append(
                f"Skipped {library.unavailable_playlists} Spotify playlist(s) whose items are unavailable."
            )

        playlist = await self.spotify.create_playlist(request.playlist_name, request.public)
        await self.spotify.add_tracks(playlist.id, uris)
        return SpotifyDiscoveryPlaylistResponse(
            playlist_id=playlist.id,
            playlist_url=playlist.url,
            playlist_name=request.playlist_name,
            public=request.public,
            source_tracks_added=len(source_uris),
            recommendations_added=len(recommendation_tracks),
            seed_counts=seed_counts,
            recommendation_quotas=actual_quotas,
            known_tracks_excluded=exclusions.excluded_count,
            unavailable_playlists=library.unavailable_playlists,
            warnings=warnings,
        )

    async def _similar_candidates(
        self,
        seeds: dict[str, list[SpotifyTrack]],
        source_keys: set[tuple[str, str]],
        exclusions: LibraryExclusions,
    ) -> dict[str, list[AggregatedCandidate]]:
        flattened = [(segment, track) for segment in SEGMENTS for track in seeds[segment]]
        if not flattened:
            return {segment: [] for segment in SEGMENTS}
        similar_lists = await get_similar_limited(
            self.lastfm,
            [(track.artist, track.name) for _, track in flattened],
            self.lastfm.config.lastfm_max_concurrency,
            self.lastfm.config.lastfm_requests_per_second,
        )
        scores: dict[str, dict[tuple[str, str], float]] = {
            segment: defaultdict(float) for segment in SEGMENTS
        }
        labels: dict[tuple[str, str], tuple[str, str]] = {}
        for (segment, _), similar_tracks in zip(flattened, similar_lists, strict=True):
            for similar in similar_tracks:
                key = track_key(similar.name, similar.artist)
                if key in source_keys:
                    continue
                if key in exclusions.track_keys:
                    exclusions.exclude_candidate(key)
                    continue
                labels[key] = (similar.name, similar.artist)
                scores[segment][key] += similar.similarity
        candidates: dict[str, list[AggregatedCandidate]] = {}
        for segment in SEGMENTS:
            total_score = sum(scores[segment].values())
            if not total_score:
                candidates[segment] = []
                continue
            candidates[segment] = sorted(
                (
                    AggregatedCandidate(*labels[key], score / total_score)
                    for key, score in scores[segment].items()
                ),
                key=lambda candidate: (
                    -candidate.score,
                    candidate.artist.casefold(),
                    candidate.name.casefold(),
                ),
            )
        return candidates

    async def _resolve_recommendations(
        self,
        candidates: dict[str, list[AggregatedCandidate]],
        initial_quotas: dict[str, int],
        source_keys: set[tuple[str, str]],
        exclusions: LibraryExclusions | None = None,
        reserved_uris: set[str] | None = None,
    ) -> tuple[list[SpotifyTrack], dict[str, int], list[str]]:
        """Fill segment quotas, transferring shortages proportionally to other segments."""
        quotas = initial_quotas.copy()
        actual = {segment: 0 for segment in SEGMENTS}
        positions = {segment: 0 for segment in SEGMENTS}
        selected: list[SpotifyTrack] = []
        selected_uris = set(reserved_uris or ())
        search_cache: dict[tuple[str, str], SpotifyTrack | None] = {}
        warnings: list[str] = []

        while True:
            shortages = 0
            for segment in SEGMENTS:
                while actual[segment] < quotas[segment]:
                    track, positions[segment] = await self._next_catalogue_track(
                        candidates[segment],
                        positions[segment],
                        source_keys,
                        exclusions,
                        selected_uris,
                        search_cache,
                    )
                    if track is None:
                        shortages += quotas[segment] - actual[segment]
                        quotas[segment] = actual[segment]
                        break
                    selected.append(track)
                    selected_uris.add(track.uri)
                    actual[segment] += 1
            if not shortages:
                break
            warnings.append("Redistributed unavailable segment recommendation quotas.")
            eligible = {
                segment: max(0, len(candidates[segment]) - positions[segment])
                for segment in SEGMENTS
            }
            if not any(eligible.values()):
                break
            redistribution = proportional_quotas(shortages, eligible)
            redistributed = sum(redistribution.values())
            if not redistributed:
                break
            for segment in SEGMENTS:
                quotas[segment] += redistribution[segment]
        return selected, actual, warnings

    async def _next_catalogue_track(
        self,
        candidates: list[AggregatedCandidate],
        position: int,
        source_keys: set[tuple[str, str]],
        exclusions: LibraryExclusions | None,
        selected_uris: set[str],
        cache: dict[tuple[str, str], SpotifyTrack | None],
    ) -> tuple[SpotifyTrack | None, int]:
        while position < len(candidates):
            candidate = candidates[position]
            position += 1
            key = track_key(candidate.name, candidate.artist)
            if exclusions and key in exclusions.track_keys:
                exclusions.exclude_candidate(key)
                continue
            if key not in cache:
                matches = await self.spotify.search_track(candidate.name, candidate.artist)
                cache[key] = None
                for track in matches:
                    if track_key(track.name, track.artist) != key or key in source_keys:
                        continue
                    if exclusions and (
                        track.id in exclusions.track_ids
                        or track_key(track.name, track.artist) in exclusions.track_keys
                    ):
                        exclusions.exclude_spotify_track(track)
                        continue
                    cache[key] = track
                    break
            track = cache[key]
            if track and track.uri not in selected_uris:
                return track, position
        return None, position

    @staticmethod
    def _unique_tracks(tracks: Iterable[SpotifyTrack]) -> list[SpotifyTrack]:
        unique: dict[str, SpotifyTrack] = {}
        for track in tracks:
            unique.setdefault(track.id, track)
        return list(unique.values())

    @staticmethod
    def _unique_uris(tracks: list[SpotifyTrack]) -> list[str]:
        return list(dict.fromkeys(track.uri for track in tracks))
