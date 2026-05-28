"""Pydantic schemas for Brief Writer (spec v1 §2)."""
from typing import Optional
from pydantic import BaseModel


class BriefRequest(BaseModel):
    brief_id: str
    thesis: str
    audience: str
    max_words: int = 350
    data_slice_sql: str
    style: str = "awareness-grade-pulse"


class Citation(BaseModel):
    claim: str
    source: str


class Brief(BaseModel):
    title: str
    body: str
    citations: list[Citation]
    drafted_at: str
    iteration: int


class GraderVerdict(BaseModel):
    passed: bool
    reasons_failed: list[str]
    suggested_revisions: list[str]
    verified_at: str


class LoopOutcome(BaseModel):
    final_status: str
    final_brief: Optional[Brief]
    verdict_history: list[GraderVerdict]
    iterations: int
    elapsed_seconds: float
