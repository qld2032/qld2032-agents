"""Pydantic schemas for Editor / Signal Detector v1.

Per editor-spec-v1.md §2.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateBrief(BaseModel):
    """A signal-detected brief topic ranked by salience."""

    brief_id: str = Field(
        ...,
        description="Registered key (matches BRIEF_ALLOWLIST) OR auto-generated "
                    "as '{scanner}_{slugify(agency)}_{date_yyyymm}'.",
    )
    thesis: str = Field(..., description="One-sentence framing for the brief.")
    scanner: str = Field(
        ...,
        description="Scanner that produced this candidate. "
                    "v1 values: 'disclosure_delta', 'threshold_crossing'.",
    )
    domain: str = Field(
        default="forward_procurement",
        description="Data domain. v1 supports forward_procurement only.",
    )
    salience_score: float = Field(
        default=0.0,
        description="Composite salience score 0.0-1.0. Set by scorer downstream.",
    )
    salience_components: dict[str, float] = Field(
        default_factory=dict,
        description="Per-weight breakdown for explainability. "
                    "Keys: delta_magnitude, brisbane_2032, dollar_tier, recency.",
    )
    suggested_data_slice_sql: str = Field(
        ...,
        description="BQ query Brief Writer would run to populate the data slice.",
    )
    delta_summary: dict = Field(
        default_factory=dict,
        description="Raw numbers showing the delta (audit trail).",
    )


class EditorOutcome(BaseModel):
    """The result of an Editor scan invocation."""

    candidates: list[CandidateBrief] = Field(default_factory=list)
    scanners_run: list[str] = Field(default_factory=list)
    total_candidates_before_ranking: int = Field(default=0)
    elapsed_seconds: float = Field(default=0.0)
    scanned_at: str = Field(..., description="ISO timestamp UTC.")
    cache_hit: bool = Field(
        default=False,
        description="True if outcome served from 60-min scan cache per spec §6.",
    )
