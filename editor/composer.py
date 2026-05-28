"""Composer: CandidateBrief → BriefRequest.

Per editor-spec-v1.md §5. Bridge between Editor signal detection
and Brief Writer's LoopAgent input.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/opt/hapi")

from brief_writer.schemas import BriefRequest

from .schemas import CandidateBrief

DEFAULT_AUDIENCE = "Queensland construction supply-chain practitioners"
DEFAULT_MAX_WORDS = 350
DEFAULT_STYLE = "awareness-grade-pulse"


def compose_brief_request(
    candidate: CandidateBrief,
    audience: str = DEFAULT_AUDIENCE,
    max_words: int = DEFAULT_MAX_WORDS,
    style: str = DEFAULT_STYLE,
) -> BriefRequest:
    """Convert a CandidateBrief into a BriefRequest for Brief Writer's LoopAgent."""
    return BriefRequest(
        brief_id=candidate.brief_id,
        thesis=candidate.thesis,
        audience=audience,
        max_words=max_words,
        data_slice_sql=candidate.suggested_data_slice_sql,
        style=style,
    )
