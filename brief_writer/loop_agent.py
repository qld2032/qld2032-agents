"""LoopAgent orchestrator -- Writer → Grader → retry, max 3 iterations.

Spec v1 §5. Snapshot (data + substrate) fetched once at start. Writer + Grader
called per iteration. Final brief emitted to /opt/hapi/data/briefs/staged/.
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from brief_writer.schemas import BriefRequest, LoopOutcome
from brief_writer.tools import bq_query_local, search_substrate_local
from brief_writer.writer import Writer
from brief_writer.grader import Grader


STAGED_DIR = Path("/opt/hapi/data/briefs/staged")
log = logging.getLogger("brief_writer.loop_agent")


def _emit_to_staged(outcome: LoopOutcome, brief_id: str) -> Path:
    """Write final brief markdown to staged dir with frontmatter + verdict history."""
    if outcome.final_brief is None:
        log.warning("emit: no final_brief on outcome, skipping write")
        return None

    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    ts = outcome.final_brief.drafted_at.replace(":", "-").replace("+", "_")
    out_path = STAGED_DIR / f"{brief_id}-{ts}.md"

    fm = (
        "---\n"
        f"brief_id: {brief_id}\n"
        f"drafted_at: {outcome.final_brief.drafted_at}\n"
        f"final_status: {outcome.final_status}\n"
        f"iterations: {outcome.iterations}\n"
        f"elapsed_seconds: {outcome.elapsed_seconds:.2f}\n"
        f"verdict_summary: {'PASSED' if outcome.final_status == 'passed' else 'NEEDS_REVIEW'}\n"
        "---\n\n"
    )
    body = f"# {outcome.final_brief.title}\n\n{outcome.final_brief.body}\n\n"

    cites = "\n## Citations\n\n"
    for c in outcome.final_brief.citations:
        cites += f"- **{c.claim}** -- `{c.source}`\n"

    history = "\n## Verdict History\n\n"
    for i, v in enumerate(outcome.verdict_history, 1):
        history += f"### Iteration {i}: {'PASSED' if v.passed else 'FAILED'}\n"
        if not v.passed:
            history += "Reasons:\n"
            for r in v.reasons_failed:
                history += f"- {r}\n"
            if v.suggested_revisions:
                history += "\nSuggested revisions:\n"
                for s in v.suggested_revisions:
                    history += f"- {s}\n"
        history += "\n"

    out_path.write_text(fm + body + cites + history)
    log.info(f"emit: wrote final brief to {out_path}")
    return out_path


def LoopAgent(request: BriefRequest, max_iterations: int = 3) -> LoopOutcome:
    """Run Writer → Grader → retry loop. Snapshot fetched once at start.

    Returns LoopOutcome with final_status, final_brief, verdict_history,
    iterations, elapsed_seconds. Side effect: writes brief artifact to staged
    dir on completion (regardless of pass/fail outcome).
    """
    started = time.time()
    log.info(f"LoopAgent start: brief_id={request.brief_id} max_iter={max_iterations}")

    data_rows = bq_query_local(request.data_slice_sql)
    log.info(f"snapshot: {len(data_rows)} rows from BQ")
    substrate_context = search_substrate_local(request.thesis, top_k=3)
    log.info(f"snapshot: {len(substrate_context)} chars of substrate context")

    verdict_history = []
    prior_verdict = None
    final_brief = None

    for iteration in range(1, max_iterations + 1):
        log.info(f"--- iteration {iteration} ---")
        brief = Writer(request, data_rows, substrate_context, prior_verdict)
        brief.iteration = iteration
        final_brief = brief
        log.info(f"Writer done (citations={len(brief.citations)})")

        verdict = Grader(brief, request, data_rows)
        verdict_history.append(verdict)
        log.info(f"Grader verdict: passed={verdict.passed} reasons={len(verdict.reasons_failed)}")

        if verdict.passed:
            outcome = LoopOutcome(
                final_status="passed",
                final_brief=brief,
                verdict_history=verdict_history,
                iterations=iteration,
                elapsed_seconds=time.time() - started,
            )
            _emit_to_staged(outcome, request.brief_id)
            log.info(f"LoopAgent PASSED at iter {iteration} in {outcome.elapsed_seconds:.2f}s")
            return outcome

        prior_verdict = verdict

    outcome = LoopOutcome(
        final_status="max_iterations_reached",
        final_brief=final_brief,
        verdict_history=verdict_history,
        iterations=max_iterations,
        elapsed_seconds=time.time() - started,
    )
    _emit_to_staged(outcome, request.brief_id)
    log.warning(f"LoopAgent did NOT pass after {max_iterations} iters in {outcome.elapsed_seconds:.2f}s")
    return outcome
