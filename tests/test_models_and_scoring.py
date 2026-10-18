from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.common.models import SpotifyDiscoveryPlaylistResponse, TopNewsRequest
from rest_service.scoring import calculate_interest_score
from rest_service.telegram_client import parse_period


def test_parse_compact_period() -> None:
    assert parse_period("1w2d3h4m") == timedelta(weeks=1, days=2, hours=3, minutes=4)


@pytest.mark.parametrize("period", ["", "0h", "two days", "1x"])
def test_period_rejects_invalid_values(period: str) -> None:
    with pytest.raises(ValidationError):
        TopNewsRequest(period=period, included_channels=["news"])


def test_inclusion_mode_requires_a_channel() -> None:
    with pytest.raises(ValidationError, match="included_channels"):
        TopNewsRequest(period="1d")


def test_exclusion_mode_allows_empty_exclusions() -> None:
    request = TopNewsRequest(period="9h32m", exclude_mode=True)
    assert request.excluded_channels == []


def test_spotify_response_exposes_library_filter_diagnostics() -> None:
    response = SpotifyDiscoveryPlaylistResponse(
        playlist_id="playlist-id",
        playlist_url="https://open.spotify.com/playlist/playlist-id",
        playlist_name="Fresh discoveries",
        public=False,
        source_tracks_added=1,
        recommendations_added=3,
        seed_counts={"short_term": 1, "medium_term": 1, "long_term": 1},
        recommendation_quotas={"short_term": 1, "medium_term": 1, "long_term": 1},
        known_tracks_excluded=4,
        unavailable_playlists=2,
    )

    assert response.model_dump(include={"known_tracks_excluded", "unavailable_playlists"}) == {
        "known_tracks_excluded": 4,
        "unavailable_playlists": 2,
    }


def test_interest_score_uses_engagement_and_freshness() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    score = calculate_interest_score(
        reactions=10,
        comments=2,
        shares=1,
        views=1_000,
        average_views=500,
        created_at=now,
        smoothing_k=100,
        decay_lambda=0.05,
        reaction_weight=1,
        comment_weight=6,
        share_weight=10,
        now=now,
    )
    older_score = calculate_interest_score(
        reactions=10,
        comments=2,
        shares=1,
        views=1_000,
        average_views=500,
        created_at=now - timedelta(hours=10),
        smoothing_k=100,
        decay_lambda=0.05,
        reaction_weight=1,
        comment_weight=6,
        share_weight=10,
        now=now,
    )
    assert score > 0
    assert older_score == pytest.approx(score * 0.6065306597)


def test_interest_score_is_zero_without_channel_baseline() -> None:
    assert (
        calculate_interest_score(
            reactions=5,
            comments=1,
            shares=0,
            views=100,
            average_views=0,
            created_at=datetime.now(UTC),
            smoothing_k=100,
            decay_lambda=0.05,
            reaction_weight=1,
            comment_weight=6,
            share_weight=10,
        )
        == 0
    )
