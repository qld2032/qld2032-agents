"""Salience composite scorer for Editor v1.

Per editor-spec-v1.md §4. Four components:
  delta_magnitude × brisbane_2032 × dollar_tier × recency
Normalized to [0.0, 1.0] by dividing by theoretical max 4.5.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .schemas import CandidateBrief

# Dollar tier integer → multiplier (per spec §4)
DOLLAR_TIER_MULTIPLIERS = {
    0: 1.0,  # unpriced
    1: 1.0,  # <$5M
    2: 1.5,  # $5M-$10M  -- spec lists 1.5 for $1M-$5M, but tier_int=1 covers that
    3: 2.0,  # $10M-$50M
    4: 2.5,  # $50M-$100M
    5: 3.0,  # $100M+
}
# Spec §4 worked example shows Stadium ($5M-$10M tier_int=2) gets multiplier 2.0.
# Spec lists: <$1M=1.0, $1M-$5M=1.5, $5M-$10M=2.0, $10M-$50M=2.5, $50M+=3.0.
# Re-aligning to spec's numbers below to match worked examples exactly.
DOLLAR_TIER_MULTIPLIERS_SPEC = {
    0: 1.0,
    1: 1.5,   # <$5M bucket (includes Less than $1M and $1M-$5M)
    2: 2.0,   # $5M-$10M
    3: 2.5,   # $10M-$50M
    4: 3.0,   # $50M-$100M
    5: 3.0,   # $100M+ (still capped at 3.0 per spec max)
}

# Theoretical max raw score for normalization (per spec §4)
RAW_MAX = 1.0 * 1.5 * 3.0 * 1.0  # 4.5


def _delta_magnitude(candidate: CandidateBrief) -> float:
    """Scanner-specific delta magnitude in [0.0, 1.0]."""
    if candidate.scanner == "disclosure_delta":
        return min(candidate.delta_summary.get("delta_pct", 0.0) / 100.0, 1.0)
    if candidate.scanner == "threshold_crossing":
        n = candidate.delta_summary.get("new_total", 0) or 0
        return min(math.log(1 + n) / math.log(1 + 100), 1.0)
    return 0.0


def _recency_multiplier(latest_ingested_at: str | None) -> float:
    """1.0 if <=7 days, 0.8 if <=30 days, 0.5 otherwise."""
    if not latest_ingested_at:
        return 0.8
    try:
        ts = datetime.fromisoformat(latest_ingested_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.8
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days_since = (datetime.now(timezone.utc) - ts).days
    if days_since <= 7:
        return 1.0
    if days_since <= 30:
        return 0.8
    return 0.5


def score_candidate(candidate: CandidateBrief) -> tuple[float, dict]:
    """Compute composite salience and per-weight breakdown.

    Returns (normalized_score, components_dict).
    """
    components = {
        "delta_magnitude": _delta_magnitude(candidate),
        "brisbane_2032": 1.5 if candidate.delta_summary.get("has_brisbane_2032") else 1.0,
        "dollar_tier": DOLLAR_TIER_MULTIPLIERS_SPEC.get(
            int(candidate.delta_summary.get("highest_dollar_tier_int", 0) or 0),
            1.0,
        ),
        "recency": _recency_multiplier(candidate.delta_summary.get("latest_ingested_at")),
    }
    raw = (
        components["delta_magnitude"]
        * components["brisbane_2032"]
        * components["dollar_tier"]
        * components["recency"]
    )
    normalized = min(raw / RAW_MAX, 1.0)
    return normalized, components
