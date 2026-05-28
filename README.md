# qld2032-agents

The agent layer behind **[QLD2032](https://qld2032.com)** — a production *System of Action* for Queensland construction-procurement intelligence.

Multi-model agents (Claude + Gemini) operate over a BigQuery + Gemini File Search substrate, exposed through an OAuth-protected MCP gateway, with a Writer/Grader verification loop that refuses to publish unverifiable claims. Built and operated independently by a solo founder on a sub-$100/month GCP run rate.

> **Google for Startups AI Agents Challenge — Track 2 (Optimize Existing Agents).**
> Live platform: https://qld2032.com · Demo video and architecture diagram in the Devpost submission.

Licensed under **Apache 2.0**.

---

## What this is

QLD2032 consolidates 35+ fragmented Queensland government data sources into a single queryable substrate, then runs agents over it that detect, draft, and self-verify procurement-intelligence briefs before a human publishes them.

This repository contains the **agent layer** — the Editor, the Brief Writer verification loop, and the hAPI MCP gateway that exposes them. It does **not** contain secrets, private data, or commercial niche configuration (see *What's not here*).

## Architecture

Four layers, mapping to the Agentic Data Cloud pattern:

```
DATA SOURCES        60+ QLD government feeds (TMR, council DAs, QBCC, QTenders,
                    VendorPanel, ABN, ABS, ARENA, G-NAF, DCDB, Valuer-General …)
       │            watcher cron → chef adapter (ingestion)
       ▼
SUBSTRATE           BigQuery (structured, auditable) + Gemini File Search (RAG)
       │            dual-destination: rows → BigQuery, docs → File Search
       ▼
MCP GATEWAY (hAPI)  OAuth 2.1 · 14 tools · human-gated writes · BYO-MCP
       │
       ▼
AI COUNCIL          Claude Opus 4.7 (reasoning/orchestration) ·
                    Gemini 2.5 Pro (Writer) · Gemini 2.5 Flash (Grader)
       │            Editor → Brief Writer · Catch + Self-Correct
       ▼
SURFACES            qld2032.com radars + 148K-business contractor directory
```

Substrate scale (verified against live BigQuery): the complete 20.1M-record Australian
Business Number register cross-referenced against 3.5M G-NAF addresses, 3.5M DCDB
cadastral lots, 1.8M Valuer-General records, 530K historical procurement contracts,
196K QBCC licence records, 148K cross-matched businesses, and 12,342 first-seen
council development applications across 13 councils.

## The verification loop (Catch + Self-Correct)

The differentiator. The Brief Writer is not a single prompt — it is a loop:

1. **Writer** (Gemini 2.5 Pro) drafts a citation-grounded brief from the substrate.
2. **Grader** (Gemini 2.5 Flash) checks it against six rules — five mechanical
   (citations present, figures traceable, no fabricated entities, etc.) plus one
   editorial completeness rule (claims must be framed against a baseline).
3. **LoopAgent** iterates Writer → Grader up to three times. If the Grader rejects,
   the Writer must revise; if it cannot pass, the brief escalates to a human instead
   of publishing.

In the demo this caught the Writer's *own* framing failure twice on one brief
("113 new opportunities" with no baseline), forced a reframe, and passed on
iteration 3 — while a second brief passed clean on the first try. Nothing reaches
publication unverified.

## Repository layout

```
qld2032-agents/
├── README.md
├── LICENSE
├── .gitignore
├── editor/
│   ├── scanners.py          # editor_scan: ranks CandidateBriefs by salience
│   ├── scorer.py
│   ├── composer.py
│   ├── editor.py
│   └── schemas.py
├── brief_writer/
│   ├── schemas.py           # CandidateBrief / Brief / LoopOutcome models
│   ├── tools.py             # bq_query + search_substrate wrappers
│   ├── writer.py            # Gemini 2.5 Pro drafting
│   ├── grader.py            # Gemini 2.5 Flash, six-rule rubric
│   └── loop_agent.py        # Writer → Grader → retry (max 3)
├── adk/
│   └── brief_writer_adk/    # ADK Phase 1 port of the loop
└── hapi/
    └── server.py            # MCP gateway (14 tools, OAuth 2.1)
```

## The MCP gateway (hAPI)

An OAuth 2.1-protected MCP server running as a hardened systemd service on a single
GCP Compute Engine VM. BYO-MCP compatible — the same interface serves human-in-the-loop
chat, ADK-orchestrated agents, and future Gemini Enterprise consumers. Fourteen tools:

- **Substrate access** — `bq_query`, `bq_insert`, `search_substrate`, `list_recaps`, `vm_read_file`
- **Grounded reasoning** — `ask_gemini`
- **Intelligence chain** — `editor_scan`, `trigger_brief`, `trigger_refresh`, `watcher_add_source`, `ingest_to_substrate`
- **Human-gated writes** — `write_file`, `approve_write`, `reject_write`

## Security model

- **OAuth 2.1** on the gateway; no anonymous access.
- **Human-in-the-loop on every write.** `write_file` only *stages* a proposal (with an
  AST tripwire scan for dangerous imports/calls); a human must `approve_write`, and even
  then the artifact is inert until manually added to a hardcoded allowlist.
- **Read-only service account** for the substrate; writes are namespaced (`chef_*`) and audited.
- **Allowlists** gate which brief topics and refresh sources can run — no open-ended execution.

## Running it

This repo is the agent code, not a turnkey deploy. You supply your own GCP project,
BigQuery dataset, Gemini API access, and OAuth credentials via environment / secret
files that are **not** committed (see `.gitignore`). Wire `bq_query` / `search_substrate`
to your own substrate before the loop will produce anything.

## What's not here

By design, this repository excludes:

- **All secrets** — service-account keys, API keys, OAuth client secrets, bearer tokens.
- **Private data** — substrate dumps, File Search store contents, recap archive.
- **Commercial niche configuration** — the domain-specific surfaces and packaging that
  constitute the platform's moat.

The agent architecture is open; the data and the niches are not.

## Status

- Editor → Brief Writer verification loop: **live in production.**
- ADK Phase 1: Brief Writer chain ported to ADK classes and running.
- Chef (substrate ingestion) and Watcher (drift detection) ADK migration: in progress.

## License

Apache License 2.0 — see [LICENSE](./LICENSE). © 2026 Christopher Savins.
