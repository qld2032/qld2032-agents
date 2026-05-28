"""ADK port of Brief Writer's Writer + Grader + LoopAgent pattern.

Per editor-spec-v1.md derived chain: Writer → Grader → exit_loop on PASS.
Maximum 3 iterations safety limit. Matches plain Python LoopAgent semantics.
"""
from __future__ import annotations

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.tools import ToolContext


WRITER_INSTRUCTION = """You are a procurement intelligence brief writer for the Queensland construction industry.

THESIS: {thesis}

DATA ROWS (ground truth — every numeric claim must trace back to one of these rows):
{data_rows_formatted}

SUBSTRATE CONTEXT (background framing only — never source for numeric claims):
{substrate_context}

PRIOR GRADER FEEDBACK (empty on iteration 1; populated when revising):
{grader_verdict}

INSTRUCTIONS:
- Write a 250-350 word brief.
- Awareness-grade language only: no "should bid", "this is profitable", "recommended", or any prescriptive commercial advice.
- End the body with a horizontal rule and the verbatim disclaimer: *Disclaimer: This information is for awareness purposes only. Verify all data with the originating source before any commercial use.*
- Cite every numeric claim with a specific data row reference.

If PRIOR GRADER FEEDBACK is non-empty, your task is REVISION. Address each listed reason_failed exactly. Do NOT introduce new numeric claims or new citations beyond the ones present in the prior draft. Keep the same overall framing.

OUTPUT FORMAT (STRICTLY JSON, no markdown fences, no preamble):
A single JSON object with three keys:
  title (string)
  body (string — the markdown body including disclaimer)
  citations (array of objects, each with two keys: claim and source)
"""


GRADER_INSTRUCTION = """You are a strict editorial verifier for procurement intelligence briefs.

BRIEF TO EVALUATE (JSON):
{current_draft}

DATA ROWS (ground truth):
{data_rows_formatted}

EVALUATE AGAINST THESE 6 RULES:

Rule 1 - NUMERIC ACCURACY: every numeric claim in the brief body must match a row in DATA ROWS.
Rule 2 - NO DECISION-GRADE LANGUAGE: no "should bid", "this is profitable", "recommended", or any prescriptive commercial advice.
Rule 3 - CITATIONS RESOLVE: every citation source must map to a row in DATA ROWS.
Rule 4 - CURRENCY: if data_rows include chef_ingested_at, it must be within 35 days. Auto-pass if the field is absent.
Rule 5 - VERIFICATION DISCLAIMER: the "for awareness purposes only" disclaimer must be present at the end of the body.
Rule 6 - FRAMING COMPLETENESS: if the brief reports a reduction or shift in a subset, the parent set total must be explicitly acknowledged so proportions are clear.

DECISION:
- If ALL 6 rules pass: call the exit_loop tool. This terminates the refinement loop with PASS status. Do not output any other content.
- If ANY rule fails: do NOT call exit_loop. Instead output JSON only (no fences) with three keys:
  passed (false)
  reasons_failed (array of strings like "Rule X: brief explanation")
  suggested_revisions (array of specific actionable strings the Writer should address)
"""


def exit_loop(tool_context: ToolContext) -> dict:
    """Signal that the brief has passed all 6 grader rules. Terminates the LoopAgent."""
    tool_context.actions.escalate = True
    return {"status": "All 6 rules passed. Brief approved. Loop terminating."}


writer = LlmAgent(
    name="Writer",
    model="gemini-2.5-pro",
    instruction=WRITER_INSTRUCTION,
    output_key="current_draft",
)

grader = LlmAgent(
    name="Grader",
    model="gemini-2.5-flash",
    instruction=GRADER_INSTRUCTION,
    tools=[exit_loop],
    output_key="grader_verdict",
)

root_agent = LoopAgent(
    name="BriefRefinementLoop",
    sub_agents=[writer, grader],
    max_iterations=3,
)
