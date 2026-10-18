"""Interest Score calculation from METRIC.md."""

import math
from datetime import UTC, datetime


def calculate_interest_score(
    *,
    reactions: int,
    comments: int,
    shares: int,
    views: int,
    average_views: float,
    created_at: datetime,
    smoothing_k: float,
    decay_lambda: float,
    reaction_weight: float,
    comment_weight: float,
    share_weight: float,
    now: datetime | None = None,
) -> float:
    """Calculate a primarily engagement-weighted score for one Telegram post."""
    if average_views <= 0:
        return 0.0

    engagement = (
        reaction_weight * max(0, reactions)
        + comment_weight * max(0, comments)
        + share_weight * max(0, shares)
    )
    adjusted_er = engagement / (max(0, views) + smoothing_k)
    virality_log = math.log2(1 + max(0, views) / average_views)

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600)

    return adjusted_er * virality_log * math.exp(-decay_lambda * age_hours)
