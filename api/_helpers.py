"""Shared helpers for Vercel API routes."""

from __future__ import annotations

import cgi
import json
import os
import sys
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


def get_storage(account_id: str) -> SupabaseStorage:
    """Create a SupabaseStorage instance for the given account."""
    return SupabaseStorage(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_SERVICE_KEY"],
        account_id=account_id,
    )


def get_pipeline(account_id: str) -> EnrichmentPipeline:
    """Create an EnrichmentPipeline with per-account API keys.

    Uses /tmp for temp file processing. Persistence goes through
    SupabaseStorage, not the pipeline's file-based save/load.
    """
    client = get_supabase_client()
    keys = get_account_keys(client, account_id)

    # Set API keys in environment for sub-modules that read os.environ
    for k, v in keys.items():
        if v:
            os.environ[k] = v

    return EnrichmentPipeline(
        data_dir="/tmp/ps-datasets",
        enrichlayer_api_key=keys.get("ENRICHLAYER_API_KEY", ""),
    )


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
