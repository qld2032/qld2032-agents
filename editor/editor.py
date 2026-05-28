"""Editor entry point — scan_for_deltas.

Per editor-spec-v1.md §6. Orchestrates scanners, applies scorer,
dedupes within-scanner, caches outcome for 60 minutes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .schemas import CandidateBrief, EditorOutcome
from .scanners import REGISTERED_SCANNERS
from .scorer import score_candidate

log = logging.getLogger("hapi.editor")

_SCAN_CACHE: dict = {}
_SCAN_CACHE_TTL_SECONDS = 60 * 60  # 60 minutes per spec §10.5


def _dedupe_within_scanner(candidates: list[CandidateBrief]) -> list[CandidateBrief]:
    """Keep highest-scoring candidate per (scanner, agency) tuple.

    Cross-scanner same-agency stays as separate candidates per spec §10.4.
    """
    best: dict[tuple[str, str], CandidateBrief] = {}
    for c in candidates:
        agency = c.delta_summary.get("agency", "_unknown")
        key = (c.scanner, agency)
        if key not in best or c.salience_score > best[key].salience_score:
            best[key] = c
    return list(best.values())


def scan_for_deltas(
    domains: list[str] | None = None,
    top_n: int = 5,
) -> EditorOutcome:
    """Run all scanners, score, dedupe, return top-N candidates."""
    if domains is None:
        domains = ["forward_procurement"]

    cache_key = (tuple(sorted(domains)), top_n)
    now_ts = time.time()

    if cache_key in _SCAN_CACHE:
        cached_outcome, cached_at = _SCAN_CACHE[cache_key]
        if now_ts - cached_at < _SCAN_CACHE_TTL_SECONDS:
            log.info("editor cache HIT for %s", cache_key)
            cached_outcome.cache_hit = True
            return cached_outcome

    started = time.time()
    all_candidates: list[CandidateBrief] = []
    scanners_run: list[str] = []

    for scanner in REGISTERED_SCANNERS:
        scanner_candidates = scanner.scan(domains)
        all_candidates.extend(scanner_candidates)
        scanners_run.append(scanner.name)

    # Apply scoring
    for c in all_candidates:
        score, components = score_candidate(c)
        c.salience_score = round(score, 3)
        c.salience_components = {k: round(v, 3) for k, v in components.items()}

    # Dedupe within (scanner, agency)
    deduped = _dedupe_within_scanner(all_candidates)
    deduped.sort(key=lambda c: c.salience_score, reverse=True)
    top = deduped[:top_n]

    outcome = EditorOutcome(
        candidates=top,
        scanners_run=scanners_run,
        total_candidates_before_ranking=len(all_candidates),
        elapsed_seconds=round(time.time() - started, 2),
        scanned_at=datetime.now(timezone.utc).isoformat(),
        cache_hit=False,
    )
    _SCAN_CACHE[cache_key] = (outcome, now_ts)
    return outcome
