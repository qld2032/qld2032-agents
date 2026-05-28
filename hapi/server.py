#!/usr/bin/env python3
"""hAPI / MCP server for qld2032 — VM-resident, OAuth 2.1 protected.

Read-only tools:        bq_query, vm_read_file, list_recaps, search_substrate
Grounded model access:  ask_gemini
Bounded action tools:   trigger_refresh, watcher_add_source
Human-gated code stage: write_file -> approve_write / reject_write

OAuth 2.1 with DCR + PKCE via mcp.server.auth + HapiOAuthProvider.
Pre-shared password gate at /login + scope consent at /consent.
"""
import os
import re
import ast
import json
import secrets
import logging
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from google.cloud import bigquery
from google import genai
from google.genai import types
from google.oauth2 import service_account
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from oauth_provider import HapiOAuthProvider
from login_routes import LoginRoutes
import subprocess
import threading
import yaml
from datetime import timezone

# ---- config ----
load_dotenv("/opt/hapi/.env")
BIND_HOST    = os.environ.get("MCP_BIND_HOST", "127.0.0.1")
BIND_PORT    = int(os.environ.get("MCP_BIND_PORT", "8000"))
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ["BQ_DATASET"]
BQ_LOCATION  = os.environ.get("BQ_LOCATION", "US")
BQ_KEY_PATH  = os.environ["BQ_KEY_PATH"]
GEMINI_KEY_PATH = os.environ.get("GEMINI_KEY_PATH", "/opt/hapi/secrets/gemini-key.txt")
RECAPS_PATH  = Path(os.environ["RECAPS_PATH"]).resolve()
LOG_DIR      = Path(os.environ["LOG_DIR"])

OAUTH_LOGIN_PASSWORD = os.environ["OAUTH_LOGIN_PASSWORD"]
OAUTH_COOKIE_SECRET  = os.environ["OAUTH_COOKIE_SECRET"]
OAUTH_DB_PATH        = os.environ.get("OAUTH_DB_PATH", "/opt/hapi/oauth.db")
OAUTH_ISSUER_URL     = os.environ["OAUTH_ISSUER_URL"]

# ---- logging ----
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "hapi.log"), logging.StreamHandler()],
)
log = logging.getLogger("hapi")

# ---- BigQuery client ----
creds = service_account.Credentials.from_service_account_file(BQ_KEY_PATH)
bq = bigquery.Client(project=BQ_PROJECT, location=BQ_LOCATION, credentials=creds)

# ---- Gemini API key ----
try:
    with open(GEMINI_KEY_PATH, "r") as f:
        GEMINI_API_KEY = f.read().strip()
    log.info("Gemini API key loaded")
except (FileNotFoundError, PermissionError) as e:
    log.warning(f"Gemini API key unavailable: {e} -- ask_gemini tool will return errors")
    GEMINI_API_KEY = None

# ---- Allowlist + DML blocker ----
READ_ALLOWLIST = [RECAPS_PATH, LOG_DIR.resolve()]
DML_RX = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|merge|grant|revoke|call)\b",
    re.IGNORECASE,
)

def _path_in_allowlist(path_str: str):
    try:
        p = Path(path_str).resolve()
    except (OSError, RuntimeError):
        return None
    for root in READ_ALLOWLIST:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    return None

def _json_safe(v):
    if isinstance(v, (datetime, date)): return v.isoformat()
    if isinstance(v, bytes): return v.decode("utf-8", errors="replace")
    return v

# ---- Watcher / trigger_refresh registry ----
WATCHER_YAML = Path("/opt/hapi/data/watcher.yaml")
SOURCE_ALLOWLIST = {
    "da-of-day": {
        "script": "/opt/hapi/scripts/da_of_the_day.py",
        "timeout_seconds": 600,
    },
    "chef-fpp": {
        "script": "/opt/hapi/scripts/chef_forward_procurement_pipeline.py",
        "timeout_seconds": 600,
    },
}
_watcher_lock = threading.Lock()
CRON_FIELD_RX = re.compile(r"^[\d\*\-\,\/]+$")

def _validate_cron(expr):
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    return all(CRON_FIELD_RX.match(f) for f in fields)

def _load_watcher():
    if not WATCHER_YAML.exists():
        return {"sources": {}}
    with WATCHER_YAML.open("r") as f:
        data = yaml.safe_load(f) or {}
    if "sources" not in data or data["sources"] is None:
        data["sources"] = {}
    return data

def _save_watcher(data):
    WATCHER_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHER_YAML.with_suffix(".yaml.tmp")
    with tmp.open("w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    tmp.replace(WATCHER_YAML)

# ---- Substrate store resolution ----
# Two-layer: alias dict for ergonomics, regex allowlist as security boundary.
# Adding a new content vertical = edit SUBSTRATE_STORE_PATTERNS. Adding a new
# store inside an existing vertical = pass fully-qualified name, no code change.
SUBSTRATE_STORE_ALIASES = {
    "dev-memory": "fileSearchStores/qld2032recapsv1-yva2wje3czzb",
}

SUBSTRATE_STORE_PATTERNS = [
    re.compile(r"^fileSearchStores/qld2032recapsv1-[a-z0-9]+$"),
    re.compile(r"^fileSearchStores/qld2032-regulatory-[a-z0-9-]+$"),
    re.compile(r"^fileSearchStores/qld2032-tenders-[a-z0-9-]+$"),
    re.compile(r"^fileSearchStores/qld2032-da-[a-z0-9-]+$"),
]

def _resolve_store_name(store_name: str) -> str:
    """Resolve alias or fully-qualified store name; validate against allowlist.
    Raises ValueError on unknown alias or pattern mismatch."""
    if not store_name or not isinstance(store_name, str):
        raise ValueError(
            f"store_name must be a non-empty string. "
            f"Available aliases: {sorted(SUBSTRATE_STORE_ALIASES.keys())}"
        )
    resolved = SUBSTRATE_STORE_ALIASES.get(store_name, store_name)
    for rx in SUBSTRATE_STORE_PATTERNS:
        if rx.match(resolved):
            return resolved
    raise ValueError(
        f"store_name {store_name!r} resolves to {resolved!r} which does not "
        f"match any SUBSTRATE_STORE_PATTERNS. Available aliases: "
        f"{sorted(SUBSTRATE_STORE_ALIASES.keys())}"
    )

# ---- write_file staging (human-gated code proposals) ----
# Everything here lives under /opt/hapi/data/ -- the only scripts-relevant path
# the systemd unit grants as writable (ReadWritePaths). The hardened service
# deliberately cannot write into /opt/hapi/scripts/ (the trusted execution dir),
# so agent-authored code is quarantined to /opt/hapi/data/staged_scripts/ and is
# still inert until a human adds it to SOURCE_ALLOWLIST above by hand.
PENDING_DIR        = Path("/opt/hapi/data/pending")
STAGED_SCRIPTS_DIR = Path("/opt/hapi/data/staged_scripts")
WRITE_FILENAME_RX  = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\.py$")
PENDING_ID_RX      = re.compile(r"^[0-9a-f]{16}$")
WRITE_MAX_BYTES    = 64 * 1024

# Modules a staged script may NOT import (network / process / deserialisation).
BLOCKED_IMPORT_MODULES = {
    "subprocess", "socket", "importlib", "ctypes", "multiprocessing",
    "urllib", "http", "requests", "httpx", "ftplib", "smtplib",
    "telnetlib", "pickle", "marshal", "shelve", "pty",
}
# Builtins a staged script may NOT call.
BLOCKED_CALL_NAMES = {"eval", "exec", "compile", "__import__"}

def _scan_python_source(src: str):
    """Tripwire AST scan of proposed script content.

    Returns (ok, violations). This is NOT a sandbox -- the real safety gates
    are the human approve step and trigger_refresh's hardcoded SOURCE_ALLOWLIST.
    This just blocks the obvious dangerous shapes before a human ever sees it.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_IMPORT_MODULES:
                    violations.append(f"blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BLOCKED_IMPORT_MODULES:
                violations.append(f"blocked import: from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BLOCKED_CALL_NAMES:
                violations.append(f"blocked call: {fn.id}()")
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                if fn.value.id == "os" and (
                    fn.attr in {"system", "popen", "fork", "posix_spawn"}
                    or fn.attr.startswith("exec")
                    or fn.attr.startswith("spawn")
                ):
                    violations.append(f"blocked call: os.{fn.attr}()")
    return (len(violations) == 0), violations

# ---- OAuth setup ----
oauth_provider = HapiOAuthProvider(db_path=OAUTH_DB_PATH)

auth_settings = AuthSettings(
    issuer_url=OAUTH_ISSUER_URL,
    resource_server_url=OAUTH_ISSUER_URL,
    client_registration_options=ClientRegistrationOptions(
        enabled=True,
        valid_scopes=["mcp:full"],
        default_scopes=["mcp:full"],
    ),
    revocation_options=RevocationOptions(enabled=True),
    required_scopes=["mcp:full"],
)

# ---- MCP server ----
mcp = FastMCP(
    "hapi",
    instructions="QLD2032 substrate access: BigQuery (read-only), recap reads, substrate search.",
    auth_server_provider=oauth_provider,
    auth=auth_settings,
    transport_security=TransportSecuritySettings(
        allowed_hosts=["mcp.qld2032.com", "127.0.0.1:8000", "localhost:8000"],
    ),
)

@mcp.tool()
def bq_query(sql: str, max_rows: int = 100) -> dict:
    """Run a read-only BigQuery query against qld2032-brain. Blocks DML/DDL."""
    if DML_RX.search(sql):
        return {"error": "DML/DDL blocked. Read-only queries only."}
    log.info("bq_query: %s", sql[:200])
    try:
        job = bq.query(sql, job_config=bigquery.QueryJobConfig(
            use_query_cache=True,
            maximum_bytes_billed=5 * 1024 * 1024 * 1024,
        ))
        rows = []
        for row in job.result(max_results=max_rows):
            rows.append({k: _json_safe(v) for k, v in dict(row).items()})
        return {
            "row_count": len(rows),
            "bytes_processed": job.total_bytes_processed,
            "rows": rows,
        }
    except Exception as e:
        log.exception("bq_query failed")
        return {"error": str(e)}

# ---- BQ write side: bq_insert (chef_* tables only) ----
# Schema cache eliminates per-call tables.get round trip. Invalidates on
# hapi restart. chef_* tables are append-only; manual restart is the
# documented step if a schema changes (rare — new column added).
CHEF_TABLE_RX = re.compile(r"^qld_procurement\.chef_[a-z_]+$")
SCHEMA_CACHE: dict = {}
BQ_INSERT_MAX_ROWS = 10_000

def _get_chef_schema(table: str):
    """Fetch and cache the schema of a chef_* table.
    Raises ValueError if table does not match CHEF_TABLE_RX."""
    if not CHEF_TABLE_RX.match(table):
        raise ValueError(
            f"table {table!r} does not match {CHEF_TABLE_RX.pattern!r}"
        )
    if table not in SCHEMA_CACHE:
        SCHEMA_CACHE[table] = bq.get_table(table).schema
    return SCHEMA_CACHE[table]


@mcp.tool()
def bq_insert(table: str, rows: list) -> dict:
    """Streaming insert into a Chef-owned BigQuery table.

    Hardcoded to qld_procurement.chef_* tables (regex enforced). Uses
    insert_rows_json() — streaming insert path only. Cannot UPDATE/DELETE/DROP
    by construction. Cap 10k rows per call. Schema fetched + cached at
    module-load granularity.

    Args:
        table: fully-qualified chef_* table name
            (e.g. "qld_procurement.chef_tmr_forward_pipeline")
        rows: list of row dicts matching table schema
    """
    log_b = logging.getLogger("hapi.bq_insert")

    if not isinstance(rows, list) or not rows:
        return {"error": "rows must be a non-empty list of dicts"}
    if len(rows) > BQ_INSERT_MAX_ROWS:
        return {"error": f"row count {len(rows)} exceeds {BQ_INSERT_MAX_ROWS} cap"}
    if not all(isinstance(r, dict) for r in rows):
        return {"error": "every row must be a dict"}

    try:
        _get_chef_schema(table)  # validates table name + warms cache
    except ValueError as e:
        log_b.warning(f"bq_insert rejected table={table!r}: {e}")
        return {"error": str(e)}
    except Exception as e:
        log_b.exception("bq_insert schema fetch failed")
        return {"error": f"schema fetch failed: {type(e).__name__}: {str(e)[:200]}"}

    log_b.info(f"bq_insert: table={table} rows={len(rows)} caller=chef")

    try:
        errors = bq.insert_rows_json(table, rows)
    except Exception as e:
        log_b.exception("bq_insert insert_rows_json failed")
        return {"error": f"insert failed: {type(e).__name__}: {str(e)[:200]}"}

    timestamp = datetime.now(timezone.utc).isoformat()
    if errors:
        log_b.warning(f"bq_insert errors: table={table} sample={errors[:1]}")
        return {
            "table": table,
            "rows_inserted": 0,
            "insert_errors": errors,
            "timestamp": timestamp,
        }

    return {
        "table": table,
        "rows_inserted": len(rows),
        "insert_errors": [],
        "timestamp": timestamp,
    }


@mcp.tool()
def vm_read_file(path: str, max_bytes: int = 200_000) -> dict:
    """Read a text file from a whitelisted directory on the VM."""
    p = _path_in_allowlist(path)
    if p is None:
        return {"error": f"Path not in allowlist. Roots: {[str(r) for r in READ_ALLOWLIST]}"}
    if not p.exists(): return {"error": f"File not found: {p}"}
    if not p.is_file(): return {"error": f"Not a file: {p}"}
    log.info("vm_read_file: %s", p)
    size = p.stat().st_size
    with p.open("r", errors="replace") as f:
        content = f.read(max_bytes)
    return {"path": str(p), "size_bytes": size, "truncated": size > max_bytes, "content": content}

@mcp.tool()
def list_recaps() -> dict:
    """List all files in the recap substrate archive with name, size, mtime."""
    files = []
    for p in sorted(RECAPS_PATH.iterdir()):
        if p.is_file():
            st = p.stat()
            files.append({
                "name": p.name, "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
    log.info("list_recaps: %d files", len(files))
    return {"count": len(files), "files": files}

@mcp.tool()
def search_substrate(
    query: str,
    store_name: str = "dev-memory",
    top_k: int = 5,
    metadata_filter: str = None,
) -> dict:
    """Substrate semantic search via Gemini API File Search.

    Queries a parameterised File Search store and returns citation-grounded
    synthesis with source filenames. Default store is "dev-memory" (the
    institutional recap archive). End-user agents MUST pass store_name
    explicitly per chef-spec-v1 §7.

    Args:
        query: natural-language question
        store_name: alias ("dev-memory") or fully-qualified resource name.
            Resolved via SUBSTRATE_STORE_ALIASES + SUBSTRATE_STORE_PATTERNS.
        top_k: max grounding chunks to retrieve (default 5)
        metadata_filter: optional File Search metadata filter expression
    """
    log_s = logging.getLogger("hapi.substrate")

    if not GEMINI_API_KEY:
        return {"error": "Gemini API key not loaded at startup"}

    try:
        store = _resolve_store_name(store_name)
    except ValueError as e:
        log_s.warning(f"search_substrate rejected store_name={store_name!r}: {e}")
        return {"error": str(e)}

    model_name = "gemini-2.5-flash"
    log_s.info(
        f"search_substrate: query={query!r} store_name={store_name!r} "
        f"resolved={store!r} top_k={top_k} metadata_filter={metadata_filter!r}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        fs_kwargs = {
            "file_search_store_names": [store],
            "top_k": top_k,
        }
        if metadata_filter:
            fs_kwargs["metadata_filter"] = metadata_filter

        config = types.GenerateContentConfig(
            tools=[
                types.Tool(file_search=types.FileSearch(**fs_kwargs))
            ]
        )

        response = client.models.generate_content(
            model=model_name,
            contents=query,
            config=config,
        )

        text = response.text or ""

        # Extract cited filenames + metadata from grounding chunks
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
                        cm = getattr(rc, "custom_metadata", None) or []
                        meta = {m.key: m.string_value for m in cm if m.key}
                        if title and title not in seen:
                            seen.add(title)
                            sources.append({
                                "title": title,
                                "date": meta.get("date"),
                                "recap_type": meta.get("recap_type"),
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

        log_s.info(f"Response received ({len(text)} chars, {len(sources)} sources)")

        return {
            "response": text,
            "model": model_name,
            "store_name": store_name,
            "store": store,
            "sources": sources,
            "tokens": tokens,
        }

    except Exception as e:
        log_s.error(f"search_substrate failed: {type(e).__name__}: {e}")
        return {"error": f"search_substrate failed: {type(e).__name__}: {str(e)[:200]}"}

# ---- Substrate write side: ingest_to_substrate ----
INGEST_STAGING_DIRS = (
    Path("/opt/hapi/data/staging"),
    Path("/opt/hapi/data/chef"),
)
INGEST_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
}
INGEST_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

@mcp.tool()
def ingest_to_substrate(
    file_path: str,
    store_name: str,
    custom_metadata: dict = None,
) -> dict:
    """Ingest a single file into a Gemini File Search store.

    Path-restricted to /opt/hapi/data/staging/ and /opt/hapi/data/chef/.
    Store name resolved + validated via _resolve_store_name (Patch 1).
    50 MB cap. MIME whitelisted by suffix. Auto-injects filename + mtime
    as metadata; caller-supplied custom_metadata merged on top (strings only).

    Args:
        file_path: absolute path under staging or chef dirs
        store_name: alias or full resource name (required, no default)
        custom_metadata: optional dict of {str: str} pairs
    """
    log_i = logging.getLogger("hapi.ingest")

    if not GEMINI_API_KEY:
        return {"error": "Gemini API key not loaded at startup"}

    try:
        p = Path(file_path).resolve()
    except Exception as e:
        return {"error": f"invalid file_path: {e}"}
    if not any(p == d or str(p).startswith(str(d) + os.sep) for d in INGEST_STAGING_DIRS):
        return {
            "error": f"file_path {file_path!r} is not under an allowed staging directory",
            "allowed": [str(d) for d in INGEST_STAGING_DIRS],
        }
    if not p.is_file():
        return {"error": f"file_path {file_path!r} is not a regular file"}

    size_bytes = p.stat().st_size
    if size_bytes > INGEST_MAX_BYTES:
        return {"error": f"size {size_bytes} exceeds cap {INGEST_MAX_BYTES}"}

    suffix = p.suffix.lower()
    mime = INGEST_MIME_BY_SUFFIX.get(suffix)
    if not mime:
        return {
            "error": f"suffix {suffix!r} not in allowlist",
            "allowed_suffixes": sorted(INGEST_MIME_BY_SUFFIX.keys()),
        }

    try:
        resolved_store = _resolve_store_name(store_name)
    except ValueError as e:
        log_i.warning(f"ingest_to_substrate rejected store_name={store_name!r}: {e}")
        return {"error": str(e)}

    mtime_iso = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    merged_meta = {
        "filename": p.name,
        "date": mtime_iso,
    }
    if custom_metadata:
        if not isinstance(custom_metadata, dict):
            return {"error": "custom_metadata must be a dict if provided"}
        for k, v in custom_metadata.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return {
                    "error": f"custom_metadata keys/values must be strings "
                             f"(got key={type(k).__name__} value={type(v).__name__})"
                }
        merged_meta.update(custom_metadata)

    log_i.info(
        f"ingest_to_substrate: file={p.name} size={size_bytes} mime={mime} "
        f"store_name={store_name!r} resolved={resolved_store!r}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        cm_list = [
            types.CustomMetadata(key=k, string_value=v)
            for k, v in merged_meta.items()
        ]
        cfg = types.UploadToFileSearchStoreConfig(
            display_name=p.name,
            mime_type=mime,
            custom_metadata=cm_list,
        )
        op = client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=resolved_store,
            file=str(p),
            config=cfg,
        )
    except Exception as e:
        log_i.exception("ingest_to_substrate upload failed")
        return {"error": f"upload failed: {type(e).__name__}: {str(e)[:200]}"}

    file_id = (
        getattr(op, "name", None)
        or getattr(op, "id", None)
        or str(op)
    )
    indexed_at = datetime.now(timezone.utc).isoformat()
    log_i.info(
        f"ingest_to_substrate ok: file_id={file_id} store={resolved_store} "
        f"indexed_at={indexed_at} size={size_bytes}"
    )

    return {
        "file_id": file_id,
        "store_name": store_name,
        "store": resolved_store,
        "indexed_at": indexed_at,
        "size_bytes": size_bytes,
        "status": "ok",
    }

# ---- Gemini grounded access ----
@mcp.tool()
def ask_gemini(prompt: str, mode: str = "grounded", max_tokens: int = 2048) -> dict:
    """Ask Gemini with optional Google Search grounding.

    Modes:
      grounded (default) - Google Search ON, response + web citations
      direct             - No grounding, model knowledge only

    Use grounded for current events, QLD-specific lookups, anything that
    needs real-time web context. Use direct for fast/cheap answers from
    model knowledge alone.
    """
    log_g = logging.getLogger("hapi.gemini")

    if mode not in ("grounded", "direct"):
        return {"error": f"Invalid mode '{mode}'. Must be 'grounded' or 'direct'."}

    if not GEMINI_API_KEY:
        return {"error": "Gemini API key not loaded at startup"}

    model_name = "gemini-2.5-pro"
    log_g.info(f"Calling {model_name} with mode={mode}")

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

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

        log_g.info(f"Response received ({len(text)} chars, {len(citations)} citations, tokens={tokens.get('total','n/a')})")

        return {
            "response": text,
            "model": model_name,
            "mode": mode,
            "citations": citations,
            "tokens": tokens,
        }

    except Exception as e:
        log_g.error(f"Gemini call failed: {type(e).__name__}: {e}")
        return {"error": f"Gemini call failed: {type(e).__name__}: {str(e)[:200]}"}


@mcp.tool()
def trigger_refresh(source: str) -> dict:
    """Run an allowlisted source-refresh script as a bounded subprocess.

    Available sources: da-of-day.
    Captures stdout/stderr/returncode and updates watcher.yaml's
    last_run + last_status fields for that source.
    """
    log_t = logging.getLogger("hapi.trigger")
    if source not in SOURCE_ALLOWLIST:
        return {"error": f"Source '{source}' not in allowlist. Allowed: {list(SOURCE_ALLOWLIST)}"}
    cfg = SOURCE_ALLOWLIST[source]
    script_path = cfg["script"]
    timeout = cfg["timeout_seconds"]
    if not Path(script_path).is_file():
        return {"error": f"Script not found on VM: {script_path}"}
    log_t.info(f"trigger_refresh: source={source} script={script_path}")
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = subprocess.run(
            ["/opt/hapi/venv/bin/python", script_path],
            capture_output=True, text=True, timeout=timeout,
        )
        status = "ok" if result.returncode == 0 else f"failed_rc={result.returncode}"
        finished_at = datetime.now(timezone.utc).isoformat()
        try:
            with _watcher_lock:
                data = _load_watcher()
                if source in data["sources"]:
                    data["sources"][source]["last_run"] = finished_at
                    data["sources"][source]["last_status"] = status
                    _save_watcher(data)
        except Exception as ye:
            log_t.warning(f"watcher.yaml update failed: {ye}")
        log_t.info(f"trigger_refresh done: source={source} status={status}")
        return {
            "source": source,
            "status": status,
            "returncode": result.returncode,
            "started_at": started_at,
            "finished_at": finished_at,
            "stdout": result.stdout[-8000:],
            "stderr": result.stderr[-4000:],
        }
    except subprocess.TimeoutExpired:
        log_t.error(f"trigger_refresh TIMEOUT after {timeout}s source={source}")
        return {"error": f"Script exceeded timeout {timeout}s", "source": source, "started_at": started_at}
    except Exception as e:
        log_t.exception(f"trigger_refresh failed source={source}")
        return {"error": f"{type(e).__name__}: {e}", "source": source, "started_at": started_at}

@mcp.tool()
def watcher_add_source(source: str, interval: str) -> dict:
    """Register a source for cron-scheduled refresh in watcher.yaml.

    Validates source against the allowlist and interval as a 5-field cron
    expression. Writes the registration only -- the watcher dispatcher that
    reads watcher.yaml and fires due jobs is a separate component.
    """
    log_c = logging.getLogger("hapi.watcher")
    if source not in SOURCE_ALLOWLIST:
        return {"error": f"Source '{source}' not in allowlist. Allowed: {list(SOURCE_ALLOWLIST)}"}
    if not _validate_cron(interval):
        return {"error": f"Invalid cron expression '{interval}'. Expected 5 fields (m h dom mon dow)."}
    now_iso = datetime.now(timezone.utc).isoformat()
    cfg = SOURCE_ALLOWLIST[source]
    try:
        with _watcher_lock:
            data = _load_watcher()
            existing = data["sources"].get(source, {})
            was_present = source in data["sources"]
            entry = {
                "interval": interval,
                "script": cfg["script"],
                "registered_at": existing.get("registered_at", now_iso),
                "last_run": existing.get("last_run"),
                "last_status": existing.get("last_status"),
            }
            if was_present:
                entry["updated_at"] = now_iso
            data["sources"][source] = entry
            _save_watcher(data)
        log_c.info(f"watcher_add_source: source={source} interval='{interval}'")
        return {"source": source, "entry": entry, "watcher_yaml": str(WATCHER_YAML)}
    except Exception as e:
        log_c.exception(f"watcher_add_source failed source={source}")
        return {"error": f"{type(e).__name__}: {e}"}

@mcp.tool()
def write_file(filename: str, content: str) -> dict:
    """Stage a Python source-refresh script for human review.

    This does NOT write to the live scripts directory. It validates the
    filename, size and Python syntax, runs a tripwire AST scan for dangerous
    imports/calls, then stores the proposal as an inert JSON record in the
    pending queue. Returns a pending_id and the full content for review.

    A human reads the content, then calls approve_write(pending_id) to promote
    it into /opt/hapi/data/staged_scripts/, or reject_write(pending_id) to
    discard it. Even after approval the script is NOT runnable until it is
    manually added to trigger_refresh's hardcoded SOURCE_ALLOWLIST.
    """
    log_w = logging.getLogger("hapi.write")
    if not isinstance(filename, str) or not WRITE_FILENAME_RX.match(filename):
        return {"error": "Invalid filename. Must be a plain name ending in .py "
                         "(letters, digits, _ and - only), e.g. my_source.py."}
    if not isinstance(content, str) or not content.strip():
        return {"error": "content is empty."}
    size = len(content.encode("utf-8"))
    if size > WRITE_MAX_BYTES:
        return {"error": f"content too large: {size} bytes (max {WRITE_MAX_BYTES})."}
    ok, violations = _scan_python_source(content)
    if not ok:
        log_w.warning(f"write_file REJECTED filename={filename} violations={violations}")
        return {"error": "Content failed the safety scan -- nothing was staged.",
                "violations": violations}
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        pending_id = secrets.token_hex(8)
        record = {
            "pending_id": pending_id,
            "target_filename": filename,
            "content": content,
            "size_bytes": size,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ast_scan": "passed",
        }
        rec_path = PENDING_DIR / f"{pending_id}.json"
        tmp = rec_path.parent / (rec_path.name + ".tmp")
        with tmp.open("w") as f:
            json.dump(record, f, indent=2)
        tmp.replace(rec_path)
    except Exception as e:
        log_w.exception("write_file staging failed")
        return {"error": f"{type(e).__name__}: {e}"}
    log_w.info(f"write_file STAGED pending_id={pending_id} filename={filename} size={size}")
    return {
        "pending_id": pending_id,
        "target_filename": filename,
        "size_bytes": size,
        "ast_scan": "passed",
        "status": "staged_for_review",
        "staged_content": content,
        "next_step": f"Human: review the content above, then call "
                     f"approve_write('{pending_id}') or reject_write('{pending_id}').",
    }

@mcp.tool()
def approve_write(pending_id: str) -> dict:
    """Promote a staged write_file proposal into /opt/hapi/data/staged_scripts/.

    Reads the pending JSON record, re-runs the AST safety scan, writes the
    content to the staged-scripts directory, then removes the pending record.
    The script still must be added to SOURCE_ALLOWLIST by hand before
    trigger_refresh can execute it.
    """
    log_w = logging.getLogger("hapi.write")
    if not isinstance(pending_id, str) or not PENDING_ID_RX.match(pending_id):
        return {"error": "Invalid pending_id format."}
    rec_path = PENDING_DIR / f"{pending_id}.json"
    if not rec_path.is_file():
        return {"error": f"No pending proposal with id {pending_id}."}
    try:
        with rec_path.open("r") as f:
            record = json.load(f)
    except Exception as e:
        return {"error": f"Could not read pending record: {type(e).__name__}: {e}"}
    filename = record.get("target_filename", "")
    content  = record.get("content", "")
    if not WRITE_FILENAME_RX.match(filename or ""):
        return {"error": f"Pending record has an invalid filename: {filename!r}"}
    ok, violations = _scan_python_source(content)
    if not ok:
        return {"error": "Re-scan at approval failed -- nothing was written.",
                "violations": violations}
    try:
        STAGED_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = STAGED_SCRIPTS_DIR / filename
        existed = dest.is_file()
        tmp = dest.parent / (dest.name + ".tmp")
        with tmp.open("w") as f:
            f.write(content)
        tmp.replace(dest)
        dest.chmod(0o750)
        rec_path.unlink()
    except Exception as e:
        log_w.exception("approve_write failed")
        return {"error": f"{type(e).__name__}: {e}"}
    log_w.info(f"approve_write PROMOTED pending_id={pending_id} -> {dest} (overwrote={existed})")
    return {
        "pending_id": pending_id,
        "written_to": str(dest),
        "overwrote_existing": existed,
        "size_bytes": len(content.encode("utf-8")),
        "reminder": "Not runnable via trigger_refresh until added to "
                    "SOURCE_ALLOWLIST in server.py (a manual paste-bridge edit).",
    }

@mcp.tool()
def reject_write(pending_id: str) -> dict:
    """Discard a staged write_file proposal. Writes nothing, deletes the record."""
    log_w = logging.getLogger("hapi.write")
    if not isinstance(pending_id, str) or not PENDING_ID_RX.match(pending_id):
        return {"error": "Invalid pending_id format."}
    rec_path = PENDING_DIR / f"{pending_id}.json"
    if not rec_path.is_file():
        return {"error": f"No pending proposal with id {pending_id}."}
    try:
        rec_path.unlink()
    except Exception as e:
        log_w.exception("reject_write failed")
        return {"error": f"{type(e).__name__}: {e}"}
    log_w.info(f"reject_write DISCARDED pending_id={pending_id}")
    return {"pending_id": pending_id, "status": "discarded"}


# ============================================================================
# Editor + Brief Writer MCP wrappers — added Step 3, May 24 2026
# Per editor-spec-v1.md §7. Inserted before async def health(request).
# ============================================================================

import subprocess as _subprocess
import json as _json

BRIEF_ALLOWLIST = {
    "stadium-pricing-delta",
    "tmr-brisbane-2032-emergence",
    "disclosure_delta_dept-of-housing-and-public-works_202605",
}
BRIEF_RUNNER = "/opt/hapi/scripts/briefs/_run_brief.py"
BRIEF_TIMEOUT_SECONDS = 300


@mcp.tool()
def editor_scan(domains: list[str] = None, top_n: int = 5) -> dict:
    """Run Editor's scanners across domains, return ranked CandidateBriefs.

    Each candidate is a brief topic the system detected as potentially worth
    publishing. Higher salience_score = stronger signal. Results cached for
    60 minutes per (domains, top_n) tuple to protect BigQuery compute.

    Args:
        domains: list of data domains to scan. v1 supports ["forward_procurement"].
        top_n: max number of candidates to return (default 5).
    """
    log_e = logging.getLogger("hapi.editor")
    if domains is None:
        domains = ["forward_procurement"]
    try:
        from editor.editor import scan_for_deltas
        outcome = scan_for_deltas(domains=domains, top_n=top_n)
        log_e.info(
            "editor_scan returned %d candidates (cache_hit=%s)",
            len(outcome.candidates), outcome.cache_hit,
        )
        return outcome.model_dump()
    except Exception as e:
        log_e.exception("editor_scan failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def trigger_brief(brief_id: str) -> dict:
    """Fire Brief Writer LoopAgent for a registered brief topic.

    Only pre-registered brief_ids in BRIEF_ALLOWLIST are accepted (v1 design).
    Triggers the full Writer → Grader → retry loop (max 3 iterations).
    Subprocesses to /opt/hapi/scripts/briefs/_run_brief.py with brief_id as arg.
    Returns the parsed LoopOutcome JSON on success.

    Args:
        brief_id: registered key (see BRIEF_ALLOWLIST in server.py).
    """
    log_b = logging.getLogger("hapi.brief")
    if not isinstance(brief_id, str) or not brief_id:
        return {"ok": False, "error": "brief_id must be a non-empty string"}
    if brief_id not in BRIEF_ALLOWLIST:
        return {
            "ok": False,
            "error": f"brief_id '{brief_id}' not in BRIEF_ALLOWLIST",
            "available": sorted(BRIEF_ALLOWLIST),
        }
    log_b.info("trigger_brief invoking brief_id=%s", brief_id)
    try:
        result = _subprocess.run(
            ["/opt/hapi/venv/bin/python", BRIEF_RUNNER, brief_id],
            capture_output=True,
            text=True,
            timeout=BRIEF_TIMEOUT_SECONDS,
        )
    except _subprocess.TimeoutExpired:
        return {"ok": False, "error": f"brief runner exceeded {BRIEF_TIMEOUT_SECONDS}s timeout"}
    if result.returncode != 0:
        return {
            "ok": False,
            "error": f"brief runner returncode {result.returncode}",
            "stderr": result.stderr[-2000:],
        }
    try:
        outcome = _json.loads(result.stdout)
    except _json.JSONDecodeError as e:
        return {
            "ok": False,
            "error": f"could not parse outcome JSON: {e}",
            "stdout": result.stdout[-2000:],
        }
    log_b.info("trigger_brief brief_id=%s status=%s", brief_id, outcome.get("final_status", "?"))
    return {"ok": True, "brief_id": brief_id, "outcome": outcome}


# ============================================================================
# End Editor + Brief Writer wrappers
# ============================================================================

async def health(request):
    return JSONResponse({
        "status": "ok", "service": "hapi",
        "tools": ["bq_query", "bq_insert", "vm_read_file", "list_recaps",
                  "search_substrate", "ingest_to_substrate",
                  "ask_gemini", "trigger_refresh", "watcher_add_source",
                  "write_file", "approve_write", "reject_write",
                  "editor_scan", "trigger_brief"],
        "auth": "oauth2.1",
    })

# ---- Run ----
if __name__ == "__main__":
    app = mcp.streamable_http_app()

    login_routes = LoginRoutes(
        provider=oauth_provider,
        login_password=OAUTH_LOGIN_PASSWORD,
        cookie_secret=OAUTH_COOKIE_SECRET,
    )
    for r in login_routes.routes():
        app.routes.insert(0, r)
    app.routes.insert(0, Route("/health", health, methods=["GET"]))

    log.info("hAPI/MCP starting on %s:%d (OAuth 2.1, issuer=%s)",
             BIND_HOST, BIND_PORT, OAUTH_ISSUER_URL)
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT, log_level="info")
