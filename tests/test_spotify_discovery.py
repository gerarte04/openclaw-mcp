from types import SimpleNamespace
import random

import pytest

from app.common.models import SpotifyDiscoveryPlaylistRequest
from rest_service.lastfm_client import SimilarTrack
from rest_service.spotify_client import SpotifyPlaylist, SpotifyTrack
from rest_service.spotify_discovery import (
    AggregatedCandidate,
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

    async def get_top_tracks(self, time_range: str, limit: int) -> list[SpotifyTrack]:
        return self.tracks[time_range][:limit]

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
    config = SimpleNamespace(lastfm_max_concurrency=2)

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
