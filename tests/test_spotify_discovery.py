from types import SimpleNamespace
import random

import pytest

from app.common.models import SpotifyDiscoveryPlaylistRequest
from rest_service.lastfm_client import SimilarTrack
from rest_service.spotify_client import SpotifyLibrary, SpotifyPlaylist, SpotifyTrack
from rest_service.spotify_discovery import (
    AggregatedCandidate,
    LibraryExclusions,
    SpotifyDiscoveryService,
    proportional_quotas,
    select_seed_tracks,
)


def track(track_id: str, artist: str = "Artist") -> SpotifyTrack:
    return SpotifyTrack(track_id, track_id, artist, f"spotify:track:{track_id}")


class FakeSpotify:
    def __init__(self) -> None:
        self.tracks = {
            "short_term": [track("s1"), track("s2"), track("s3"), track("s4")],
            "medium_term": [track("s1"), track("m1"), track("m2"), track("m3")],
            "long_term": [track("s2"), track("m1"), track("l1"), track("l2")],
        }
        self.added: list[str] = []
        self.library = SpotifyLibrary([], 0)

    async def get_top_tracks(self, time_range: str, limit: int) -> list[SpotifyTrack]:
        return self.tracks[time_range][:limit]

    async def get_library(self) -> SpotifyLibrary:
        return self.library

    async def search_track(self, name: str, artist: str) -> list[SpotifyTrack]:
        if name == "missing":
            return []
        return [
            SpotifyTrack(
                f"catalogue-{name}",
                name,
                artist,
                f"spotify:track:catalogue-{name}",
            )
        ]

    async def create_playlist(self, name: str, public: bool) -> SpotifyPlaylist:
        assert public is False
        return SpotifyPlaylist("playlist", "https://open.spotify.com/playlist/playlist")

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> None:
        assert playlist_id == "playlist"
        self.added = uris


class FakeLastfm:
    config = SimpleNamespace(lastfm_max_concurrency=2, lastfm_requests_per_second=1000)

    async def get_similar(self, artist: str, track_name: str) -> list[SimilarTrack]:
        if track_name.startswith("s"):
            return [SimilarTrack("short-new", "New artist", 1.0)]
        if track_name.startswith("m"):
            return [SimilarTrack("medium-new", "New artist", 1.0)]
        return [SimilarTrack("long-new", "New artist", 1.0)]


def test_seed_selection_excludes_received_earlier_ranges() -> None:
    selected = select_seed_tracks(
        [track("s1"), track("s2"), track("s3"), track("s4")],
        [track("s1"), track("m1"), track("m2"), track("m3")],
        [track("s2"), track("m1"), track("l1"), track("l2")],
        random.Random(4),
    )

    assert len(selected["short_term"]) == 2
    assert len(selected["medium_term"]) == 2
    assert {item.id for item in selected["medium_term"]}.isdisjoint({"s1", "s2", "s3", "s4"})
    assert len(selected["long_term"]) == 1
    assert selected["long_term"][0].id in {"l1", "l2"}


def test_proportional_quotas_use_largest_remainders() -> None:
    assert proportional_quotas(5, {"short_term": 4, "medium_term": 3, "long_term": 1}) == {
        "short_term": 2,
        "medium_term": 2,
        "long_term": 1,
    }


@pytest.mark.asyncio
async def test_creates_playlist_with_temporal_recommendations() -> None:
    spotify = FakeSpotify()
    service = SpotifyDiscoveryService(spotify, FakeLastfm(), random.Random(3))

    result = await service.create(
        SpotifyDiscoveryPlaylistRequest(top_n=4, playlist_name="Discoveries")
    )

    assert result.playlist_url == "https://open.spotify.com/playlist/playlist"
    assert result.source_tracks_added == 1
    assert result.recommendations_added == 3
    assert result.recommendation_quotas == {
        "short_term": 1,
        "medium_term": 1,
        "long_term": 1,
    }
    assert result.known_tracks_excluded == 0
    assert result.unavailable_playlists == 0
    assert len(spotify.added) == 4


@pytest.mark.asyncio
async def test_reassigns_unavailable_segment_quota() -> None:
    service = SpotifyDiscoveryService(FakeSpotify(), FakeLastfm(), random.Random(1))
    candidates = {
        "short_term": [
            AggregatedCandidate("short-new", "New artist", 1),
            AggregatedCandidate("another-short-new", "New artist", 0.5),
        ],
        "medium_term": [AggregatedCandidate("medium-new", "New artist", 1)],
        "long_term": [AggregatedCandidate("missing", "New artist", 1)],
    }

    tracks, actual, warnings = await service._resolve_recommendations(
        candidates,
        {"short_term": 1, "medium_term": 1, "long_term": 1},
        set(),
    )

    assert len(tracks) == 3
    assert actual["short_term"] == 2
    assert actual["long_term"] == 0
    assert warnings == ["Redistributed unavailable segment recommendation quotas."]


@pytest.mark.asyncio
async def test_does_not_count_a_source_uri_as_a_new_recommendation() -> None:
    service = SpotifyDiscoveryService(FakeSpotify(), FakeLastfm(), random.Random(1))

    tracks, actual, _ = await service._resolve_recommendations(
        {
            "short_term": [AggregatedCandidate("short-new", "New artist", 1)],
            "medium_term": [],
            "long_term": [],
        },
        {"short_term": 1, "medium_term": 0, "long_term": 0},
        set(),
        reserved_uris={"spotify:track:catalogue-short-new"},
    )

    assert tracks == []
    assert actual == {"short_term": 0, "medium_term": 0, "long_term": 0}


@pytest.mark.asyncio
async def test_excludes_liked_and_playlist_tracks_from_new_recommendations() -> None:
    service = SpotifyDiscoveryService(FakeSpotify(), FakeLastfm(), random.Random(1))
    exclusions = LibraryExclusions.from_tracks(
        [
            SpotifyTrack("liked-id", "short-new", "New artist", "spotify:track:liked-id"),
            SpotifyTrack("playlist-id", "medium-new", "New artist", "spotify:track:playlist-id"),
        ]
    )

    tracks, actual, _ = await service._resolve_recommendations(
        {
            "short_term": [AggregatedCandidate("short-new", "New artist", 1)],
            "medium_term": [AggregatedCandidate("medium-new", "New artist", 1)],
            "long_term": [AggregatedCandidate("long-new", "New artist", 1)],
        },
        {"short_term": 1, "medium_term": 1, "long_term": 1},
        set(),
        exclusions,
    )

    assert [item.name for item in tracks] == ["long-new"]
    assert actual == {"short_term": 0, "medium_term": 0, "long_term": 1}
    assert exclusions.excluded_count == 2


@pytest.mark.asyncio
async def test_uses_fresh_exact_match_after_library_match() -> None:
    spotify = FakeSpotify()

    async def search_track(name: str, artist: str) -> list[SpotifyTrack]:
        return [
            SpotifyTrack("saved-id", name, artist, "spotify:track:saved-id"),
            SpotifyTrack("fresh-id", name, artist, "spotify:track:fresh-id"),
        ]

    spotify.search_track = search_track
    service = SpotifyDiscoveryService(spotify, FakeLastfm(), random.Random(1))
    exclusions = LibraryExclusions.from_tracks([track("saved-id", "Saved artist")])

    tracks, actual, _ = await service._resolve_recommendations(
        {
            "short_term": [AggregatedCandidate("Fresh title", "New artist", 1)],
            "medium_term": [],
            "long_term": [],
        },
        {"short_term": 1, "medium_term": 0, "long_term": 0},
        set(),
        exclusions,
    )

    assert [item.id for item in tracks] == ["fresh-id"]
    assert actual == {"short_term": 1, "medium_term": 0, "long_term": 0}
    assert exclusions.excluded_count == 1


@pytest.mark.asyncio
async def test_keeps_intentionally_familiar_source_tracks() -> None:
    spotify = FakeSpotify()
    spotify.tracks = {segment: [track("s1")] for segment in spotify.tracks}
    spotify.library = SpotifyLibrary([track("s1"), track("short-new", "New artist")], 1)
    service = SpotifyDiscoveryService(spotify, FakeLastfm(), random.Random(1))

    result = await service.create(
        SpotifyDiscoveryPlaylistRequest(top_n=1, playlist_name="Familiar source is allowed")
    )

    assert spotify.added == ["spotify:track:s1"]
    assert result.source_tracks_added == 1
    assert result.recommendations_added == 0
    assert result.unavailable_playlists == 1
    assert "Skipped 1 Spotify playlist(s) whose items are unavailable." in result.warnings
