"""Shared helpers for the API routes (serves both Vercel and EC2 deploys).

EC2 caveat: the process is shared across requests, so `os.environ` is
process-wide. The env mutations below are guarded by `_keys_lock` to keep
writes atomic, but reads from sub-modules (e.g. `os.environ.get('BRAVE_API_KEY')`
in identity/enrichment code) are NOT thread-isolated. Concurrent requests
for *different accounts* would see each other's keys mid-flight. Single-
account usage (Jonah's case today) is safe because all writes are
idempotent. Multi-tenant correctness needs proper per-request key
isolation (contextvars + threaded propagation) — see MIGRATION.md.
"""

from __future__ import annotations

import cgi
import json
import os
import sys
import threading
import uuid
from pathlib import Path

# Ensure project root is on sys.path
_root = str(Path(__file__).parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from cloud.auth import (
    get_supabase_client,
    get_account_keys,
    require_auth,
    json_response,
    read_json_body,
)
from cloud.storage.supabase import SupabaseStorage
from enrichment.pipeline import EnrichmentPipeline


_keys_lock = threading.Lock()


def get_storage(account_id: str) -> SupabaseStorage:
    """Create a SupabaseStorage instance for the given account."""
    storage = SupabaseStorage(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_SERVICE_KEY"],
        account_id=account_id,
    )
    # Load account API keys into environment so Gemini/search modules can find them.
    # The lock makes the bulk write atomic so a concurrent request can't read a
    # half-updated env. It does NOT prevent cross-account leak (see module docstring).
    client = get_supabase_client()
    keys = get_account_keys(client, account_id)
    with _keys_lock:
        for k, v in keys.items():
            if v:
                os.environ[k] = v
    return storage


ENRICHMENT_KEYS = ["BRAVE_API_KEY", "SERPER_API_KEY", "ENRICHLAYER_API_KEY"]


def get_pipeline(account_id: str) -> EnrichmentPipeline:
    """Create an EnrichmentPipeline with per-account API keys.

    Falls back to environment-level keys (from Vercel env vars) if
    the account doesn't have its own. Uses /tmp for temp file processing.
    """
    client = get_supabase_client()
    keys = get_account_keys(client, account_id)

    # Fall back to env-level keys for any missing account keys
    for k in ENRICHMENT_KEYS:
        if not keys.get(k):
            keys[k] = os.environ.get(k, "")

    # Set API keys in environment for sub-modules that read os.environ.
    # See module docstring for the cross-account caveat under EC2.
    with _keys_lock:
        for k, v in keys.items():
            if v:
                os.environ[k] = v

    return EnrichmentPipeline(
        data_dir="/tmp/ps-datasets",
        enrichlayer_api_key=keys.get("ENRICHLAYER_API_KEY", ""),
    )


def check_enrichment_keys(account_id: str) -> list[str]:
    """Return list of missing enrichment API keys for this account."""
    client = get_supabase_client()
    keys = get_account_keys(client, account_id)
    missing = []
    for k in ENRICHMENT_KEYS:
        if not keys.get(k) and not os.environ.get(k):
            missing.append(k)
    return missing


def path_param(handler, position: int = -1) -> str:
    """Extract a dynamic path parameter from the request URL.

    position=-1 → last segment, -2 → second-to-last, etc.
    """
    path = handler.path.split("?")[0].rstrip("/")
    parts = path.split("/")
    return parts[position] if abs(position) <= len(parts) else ""


def parse_multipart(handler) -> tuple[dict, dict]:
    """Parse multipart/form-data. Returns (fields, files).

    files: {field_name: (filename, bytes)}
    fields: {field_name: str_value}
    """
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype:
        return {}, {}

    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": ctype,
        "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
    }
    form = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ=environ,
    )

    fields = {}
    files = {}
    for key in form:
        item = form[key]
        if isinstance(item, list):
            fields[key] = item[0].value
        elif item.filename:
            files[key] = (item.filename, item.file.read())
        else:
            fields[key] = item.value

    return fields, files


def save_temp_file(filename: str, data: bytes) -> Path:
    """Save upload data to /tmp. Returns path."""
    path = Path(f"/tmp/{uuid.uuid4().hex[:8]}_{filename}")
    path.write_bytes(data)
    return path


def count_rows(filepath: Path) -> int:
    """Count rows in a CSV or JSON file."""
    try:
        if filepath.suffix.lower() == ".json":
            with open(filepath) as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 1
        else:
            with open(filepath) as f:
                return sum(1 for _ in f) - 1
    except Exception:
        return 0
