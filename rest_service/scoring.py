"""Interest Score calculation from METRIC.md."""

from datetime import datetime, timezone
import math


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
    now: datetime | None = None,
) -> float:
    """Calculate a stable, freshness-weighted score for one Telegram post."""
    if average_views <= 0:
        return 0.0

    engagement = max(0, reactions) + 3 * max(0, comments) + 5 * max(0, shares)
    adjusted_er = engagement / (max(0, views) + smoothing_k)
    virality_log = math.log2(1 + max(0, views) / average_views)

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600)

    return adjusted_er * virality_log * math.exp(-decay_lambda * age_hours)
