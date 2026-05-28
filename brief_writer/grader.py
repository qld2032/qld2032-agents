"""Grader agent — verifies every claim in Brief against ground_truth_rows.

Spec v1 §4 + v1->v2 calibration (Rule 6 editorial completeness).
5 mechanical rules + 1 editorial-judgment rule, all evaluated by Gemini in
direct mode (no grounding, no live DB queries).
"""
import json
from datetime import datetime, timezone

from brief_writer.schemas import Brief, BriefRequest, GraderVerdict
from brief_writer.tools import ask_gemini_local


GRADER_PROMPT_TEMPLATE = """You are a strict editorial fact-checker for a procurement intelligence platform.
You verify briefs against a verified snapshot of BQ data with no tolerance for fabrication or misleading framing.

BRIEF TITLE: {title}

BRIEF BODY:
{body}

CLAIMED CITATIONS:
{citations_formatted}

GROUND TRUTH DATA (snapshot -- the entire source of numeric truth):
{ground_truth_formatted}

CHECK ALL SIX RULES:

MECHANICAL RULES (deterministic pass/fail):

1. NUMERIC ACCURACY -- Every numeric claim in the body must correspond to a value or count in GROUND TRUTH DATA. If the brief states "12 entries" the rows must contain 12 in matching context.

2. NO DECISION-GRADE LANGUAGE -- No imperatives like "you should bid", "you should buy", "this is profitable", "we recommend", "invest in" tied to specific actions. Awareness-grade only.

3. CITATIONS RESOLVE -- Every Citation.source must map to a row in GROUND TRUTH DATA. Source format is like "release_period=may-2026 spend_range=N/A n=2" -- each key=value pair should match a column=value in some row.

4. CURRENCY -- If GROUND TRUTH DATA contains a chef_ingested_at field, verify it's within 35 days of today ({today_iso}). If chef_ingested_at is NOT present in the data, treat this rule as N/A (auto-pass).

5. VERIFICATION DISCLAIMER -- Brief body must contain explicit language like "verify before commercial use", "for awareness purposes only", or equivalent.

EDITORIAL RULE (judgment-based):

6. FRAMING COMPLETENESS -- If the brief discusses changes in a subset (e.g., a count by category), it must acknowledge whether the parent set total has also changed. Failing example: "12 N/A entries reduced to 2" without noting total entries grew from 12 to 16. Misleading framing fails this rule even when sub-numbers are accurate.

Return ONLY valid JSON (no markdown fence, no commentary) matching:

{{
  "passed": boolean,
  "reasons_failed": ["specific failure for rule N: <detail>", ...],
  "suggested_revisions": ["actionable hint for Writer's next iteration", ...]
}}

If all six rules pass: passed=true, both lists empty.
If any rule fails: passed=false, one entry per failure in reasons_failed, one matching actionable hint in suggested_revisions.
"""


def _format_citations(citations) -> str:
    if not citations:
        return "(no citations)"
    return "\n".join(
        f"  - claim: {c.claim}\n    source: {c.source}"
        for c in citations
    )


def _format_rows(rows: list[dict]) -> str:
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


def Grader(
    brief: Brief,
    request: BriefRequest,
    ground_truth_rows: list[dict],
) -> GraderVerdict:
    """Verify brief against ground_truth_rows snapshot. Returns pass/fail + reasons + hints."""

    prompt = GRADER_PROMPT_TEMPLATE.format(
        title=brief.title,
        body=brief.body,
        citations_formatted=_format_citations(brief.citations),
        ground_truth_formatted=_format_rows(ground_truth_rows),
        today_iso=datetime.now(timezone.utc).isoformat(),
    )

    result = ask_gemini_local(prompt, mode="direct", max_tokens=8192, model="gemini-2.5-flash")
    raw_text = _strip_json_fence(result["response"])
    parsed = json.loads(raw_text)

    verdict = GraderVerdict(
        passed=parsed["passed"],
        reasons_failed=parsed.get("reasons_failed", []),
        suggested_revisions=parsed.get("suggested_revisions", []),
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
    return verdict
