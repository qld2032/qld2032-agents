"""Writer agent — drafts a Brief from thesis + verified data + substrate context.

Spec v1 §3. Implementation note: uses ask_gemini direct mode (not grounded) to
avoid external citation conflation. Grader strictly requires every citation to
resolve to ground_truth_rows; grounding-mode Google citations would auto-fail.
Substrate context is supplied inline as background framing.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from brief_writer.schemas import BriefRequest, Brief, GraderVerdict, Citation
from brief_writer.tools import ask_gemini_local


WRITER_PROMPT_TEMPLATE = """You are drafting a short awareness-grade industry brief for {audience}.

THESIS:
{thesis}

VERIFIED DATA (the ONLY source for numeric claims):
{data_table}

CONTEXT FROM SUBSTRATE (background framing ONLY -- DO NOT cite as a source for numeric claims):
{substrate_context}
{revision_block}
STRICT RULES:
1. Every numeric claim must cite the matching row(s) in VERIFIED DATA via the citations field
2. Substrate context is background framing only -- never the source for a numeric claim
3. Awareness-grade language only -- no "you should bid", "this is profitable", "we recommend"
4. Max {max_words} words in the body
5. Include explicit verification disclaimer at the end of the body ("verify before commercial use" or equivalent)
6. Use markdown formatting in the body

Return ONLY valid JSON (no markdown fence, no commentary) matching this schema:

{{
  "title": "string",
  "body": "string (markdown, max {max_words} words, with verification disclaimer at end)",
  "citations": [
    {{"claim": "exact sentence/phrase from body", "source": "row reference like 'release_period=may-2026 spend_range=N/A n=2'"}}
  ]
}}

The body must read naturally for the audience. The citations list must contain one entry for every numeric claim made in the body.
"""

REVISION_BLOCK_TEMPLATE = """
PREVIOUS DRAFT FAILED VERIFICATION. THIS ITERATION MUST FIX EVERY ISSUE BELOW WITHOUT INTRODUCING NEW CLAIMS OR CITATIONS.

ISSUES TO FIX:
{reasons_failed}

REQUIRED ACTIONS:
{suggested_revisions}

CRITICAL RULES FOR THIS REVISION:
- Address each flagged issue directly
- Do NOT introduce new claims or citations the previous draft did not contain
- Maintain the same citation source format as the previous draft
- Only modify wording, framing, or add minimal context to address the flagged issues
"""


def _format_rows_as_table(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    keys = list(rows[0].keys())
    widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    sep = "-+-".join("-" * widths[k] for k in keys)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))
    return "\n".join(lines)


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return t


def Writer(
    request: BriefRequest,
    data_rows: list[dict],
    substrate_context: str,
    prior_verdict: Optional[GraderVerdict] = None,
) -> Brief:
    """Draft a single Brief attempt. Caller (LoopAgent) sets iteration after return."""

    revision_block = ""
    if prior_verdict and not prior_verdict.passed:
        revision_block = REVISION_BLOCK_TEMPLATE.format(
            reasons_failed="\n".join(f"- {r}" for r in prior_verdict.reasons_failed),
            suggested_revisions="\n".join(f"- {r}" for r in prior_verdict.suggested_revisions),
        )

    data_table = _format_rows_as_table(data_rows)

    prompt = WRITER_PROMPT_TEMPLATE.format(
        audience=request.audience,
        thesis=request.thesis,
        data_table=data_table,
        substrate_context=substrate_context,
        revision_block=revision_block,
        max_words=request.max_words,
    )

    result = ask_gemini_local(prompt, mode="direct", max_tokens=16384)

    raw_text = _strip_json_fence(result["response"])
    parsed = json.loads(raw_text)

    brief = Brief(
        title=parsed["title"],
        body=parsed["body"],
        citations=[Citation(**c) for c in parsed["citations"]],
        drafted_at=datetime.now(timezone.utc).isoformat(),
        iteration=0,
    )
    return brief
