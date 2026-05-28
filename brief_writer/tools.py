"""Local helpers — in-process wrappers around google-genai + google-cloud-bigquery.

Mirrors the live hAPI MCP tools (bq_query, ask_gemini, search_substrate) but
runs without HTTP self-call overhead. Used by Writer, Grader, LoopAgent.
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from google.cloud import bigquery


# ---- Module-level singletons (lazy-init) ----
_GEMINI_API_KEY: Optional[str] = None
_BQ_CLIENT: Optional[bigquery.Client] = None

# ---- Constants ----
DML_RX = re.compile(
    r"(?i)\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|MERGE|CREATE|ALTER|GRANT|REVOKE)\b"
)

SUBSTRATE_STORE_ALIASES = {
    "dev-memory": "fileSearchStores/qld2032recapsv1-yva2wje3czzb",
}

SUBSTRATE_STORE_PATTERNS = [
    re.compile(r"^fileSearchStores/qld2032recapsv1-[a-z0-9]+$"),
    re.compile(r"^fileSearchStores/qld2032-regulatory-[a-z0-9-]+$"),
    re.compile(r"^fileSearchStores/qld2032-tenders-[a-z0-9-]+$"),
    re.compile(r"^fileSearchStores/qld2032-da-[a-z0-9-]+$"),
]

log = logging.getLogger("brief_writer.tools")


# ---- Loaders ----
def _get_gemini_key() -> str:
    global _GEMINI_API_KEY
    if _GEMINI_API_KEY:
        return _GEMINI_API_KEY
    _GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    if not _GEMINI_API_KEY:
        for p in [
            Path("/opt/hapi/secrets/gemini-key.txt"),
            Path("/var/www/qld2032/secrets/gemini-key.txt"),
        ]:
            if p.exists():
                _GEMINI_API_KEY = p.read_text().strip()
                break
    if not _GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not in env and no key file found")
    return _GEMINI_API_KEY


def _get_bq_client() -> bigquery.Client:
    global _BQ_CLIENT
    if _BQ_CLIENT is None:
        _BQ_CLIENT = bigquery.Client(project="qld2032-brain")
    return _BQ_CLIENT


def _resolve_store_name(store_name: str) -> str:
    """Resolve substrate store alias OR validate fully-qualified name."""
    if store_name in SUBSTRATE_STORE_ALIASES:
        return SUBSTRATE_STORE_ALIASES[store_name]
    if any(pat.match(store_name) for pat in SUBSTRATE_STORE_PATTERNS):
        return store_name
    raise ValueError(
        f"Unknown store '{store_name}'. Available aliases: "
        f"{list(SUBSTRATE_STORE_ALIASES)}"
    )


def _json_safe(v):
    """Best-effort JSON-safe conversion for BQ row values."""
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)


# ---- Public helpers ----
def bq_query_local(sql: str, max_rows: int = 1000) -> list[dict]:
    """Read-only BQ query against qld2032-brain. Blocks DML/DDL. Returns list of row dicts."""
    if DML_RX.search(sql):
        raise ValueError("DML/DDL not permitted -- read-only queries only")
    log.info("bq_query_local: %s", sql[:200])

    client = _get_bq_client()
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=5 * 1024 * 1024 * 1024,
        ),
    )
    rows = []
    for row in job.result(max_results=max_rows):
        rows.append({k: _json_safe(v) for k, v in dict(row).items()})
    log.info("bq_query_local: %d rows returned", len(rows))
    return rows


def ask_gemini_local(prompt: str, mode: str = "grounded", max_tokens: int = 2048, model: str = "gemini-2.5-pro") -> dict:
    """Call Gemini. Returns {response, model, mode, citations, tokens}.

    Mirrors server.py ask_gemini() exactly but raises on hard errors (caller
    decides retry / escalation).
    """
    if mode not in ("grounded", "direct"):
        raise ValueError(f"Invalid mode '{mode}'. Must be 'grounded' or 'direct'.")

    model_name = model
    log.info("ask_gemini_local: mode=%s", mode)

    client = genai.Client(api_key=_get_gemini_key())

    config_kwargs = {"max_output_tokens": max_tokens}
    if mode == "grounded":
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    config = types.GenerateContentConfig(**config_kwargs)

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=config,
    )

    text = response.text or ""

    citations = []
    if mode == "grounded":
        try:
            if response.candidates and response.candidates[0].grounding_metadata:
                gm = response.candidates[0].grounding_metadata
                if gm.grounding_chunks:
                    for chunk in gm.grounding_chunks:
                        if chunk.web:
                            citations.append({
                                "uri": chunk.web.uri,
                                "title": chunk.web.title,
                            })
        except (AttributeError, IndexError):
            pass

    tokens = {}
    try:
        if response.usage_metadata:
            tokens = {
                "input": response.usage_metadata.prompt_token_count,
                "output": response.usage_metadata.candidates_token_count,
                "total": response.usage_metadata.total_token_count,
            }
    except AttributeError:
        pass

    log.info(
        "ask_gemini_local: %d chars, %d citations, tokens=%s",
        len(text), len(citations), tokens.get("total", "n/a"),
    )

    return {
        "response": text,
        "model": model_name,
        "mode": mode,
        "citations": citations,
        "tokens": tokens,
    }


def search_substrate_local(
    query: str,
    store_name: str = "dev-memory",
    top_k: int = 5,
    metadata_filter: Optional[str] = None,
) -> str:
    """Query File Search store; return synthesised text + citation appendix.

    Returns a string (not dict) for direct use as Writer's substrate_context.
    Citations appended at end as a bracketed list.
    """
    model_name = "gemini-2.5-flash"
    store = _resolve_store_name(store_name)
    log.info(
        "search_substrate_local: query=%r store=%s top_k=%d",
        query[:100], store_name, top_k,
    )

    client = genai.Client(api_key=_get_gemini_key())

    fs_kwargs = {"file_search_store_names": [store], "top_k": top_k}
    if metadata_filter:
        fs_kwargs["metadata_filter"] = metadata_filter
    config = types.GenerateContentConfig(
        tools=[types.Tool(file_search=types.FileSearch(**fs_kwargs))],
    )

    response = client.models.generate_content(
        model=model_name,
        contents=query,
        config=config,
    )

    text = response.text or ""

    # Extract cited filenames
    sources = []
    try:
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if gm.grounding_chunks:
                seen = set()
                for chunk in gm.grounding_chunks:
                    rc = getattr(chunk, "retrieved_context", None)
                    if not rc:
                        continue
                    title = getattr(rc, "title", None)
                    if title and title not in seen:
                        seen.add(title)
                        sources.append(title)
    except (AttributeError, IndexError):
        pass

    if sources:
        text += "\n\n[Substrate citations: " + ", ".join(sources) + "]"

    log.info(
        "search_substrate_local: %d chars, %d source files",
        len(text), len(sources),
    )
    return text
