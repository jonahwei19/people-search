"""Airtable client for People Search.

Connect to an Airtable base, read records, run them through schema detection,
and optionally write enriched data back. Works standalone (no Supabase needed)
so it can be used by both the local Flask app and cloud API routes.

Rate limit: Airtable allows 5 requests/sec. All paginated fetches include
a 0.2s delay between pages.

Usage:
    from enrichment.airtable import connect, import_records, writeback

    # 1. Preview what's in the table
    info = connect(api_key, base_id, table_name)
    # info = {"columns": [...], "row_count": N, "sample_rows": [...]}

    # 2. Import all records as dicts (ready for schema detection + pipeline)
    rows = import_records(api_key, base_id, table_name)

    # 3. After enrichment, push results back
    stats = writeback(api_key, base_id, table_name, profiles, field_mapping)
    # stats = {"updated": N, "failed": N, "skipped": N}
"""

from __future__ import annotations

import time
from typing import Any

import requests

AIRTABLE_API = "https://api.airtable.com/v0"
PAGE_SIZE = 100
REQUEST_DELAY = 0.21  # just over 5 req/sec

# Columns written back to Airtable (PS_ prefix avoids collisions)
WRITEBACK_FIELDS = {
    "PS_LinkedIn_URL": "linkedin_url",
    "PS_LinkedIn_Enriched": "linkedin_enriched_text",
    "PS_Profile_Card": "profile_card",
    "PS_Enrichment_Status": "enrichment_status",
    "PS_Last_Enriched": "timestamp",
}


class AirtableError(Exception):
    """Raised on Airtable API errors."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Airtable API error {status_code}: {message}")


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _get_page(
    api_key: str,
    base_id: str,
    table_name: str,
    offset: str | None = None,
    fields: list[str] | None = None,
) -> dict:
    """Fetch one page of records from Airtable."""
    params: dict[str, Any] = {"pageSize": PAGE_SIZE}
    if offset:
        params["offset"] = offset
    if fields:
        params["fields[]"] = fields

    resp = requests.get(
        f"{AIRTABLE_API}/{base_id}/{table_name}",
        headers=_headers(api_key),
        params=params,
        timeout=30,
    )
    if resp.status_code != 200:
        raise AirtableError(resp.status_code, resp.text[:300])
    return resp.json()


def _fetch_all_records(
    api_key: str,
    base_id: str,
    table_name: str,
    fields: list[str] | None = None,
) -> list[dict]:
    """Paginate through all records in a table. Returns list of {field: value} dicts."""
    records: list[dict] = []
    offset: str | None = None

    while True:
        data = _get_page(api_key, base_id, table_name, offset=offset, fields=fields)
        for rec in data.get("records", []):
            row = rec.get("fields", {})
            row["_airtable_record_id"] = rec["id"]
            records.append(row)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(REQUEST_DELAY)

    return records


# ── Public API ────────────────────────────────────────────────


def connect(
    api_key: str, base_id: str, table_name: str
) -> dict[str, Any]:
    """Connect to an Airtable table and return schema info.

    Returns:
        {
            "columns": ["Name", "Email", ...],
            "row_count": 523,
            "sample_rows": [{"Name": "Alice", ...}, ...],  # up to 5
        }
    """
    # Fetch first page to get columns and sample data
    data = _get_page(api_key, base_id, table_name)
    raw_records = data.get("records", [])

    # Discover all columns across sample records
    columns: list[str] = []
    seen: set[str] = set()
    for rec in raw_records:
        for key in rec.get("fields", {}):
            if key not in seen:
                columns.append(key)
                seen.add(key)

    sample_rows = [rec.get("fields", {}) for rec in raw_records[:5]]

    # Count total records by paginating (just counts, doesn't store)
    total = len(raw_records)
    offset = data.get("offset")
    while offset:
        time.sleep(REQUEST_DELAY)
        page = _get_page(api_key, base_id, table_name, offset=offset)
        total += len(page.get("records", []))
        offset = page.get("offset")

    return {
        "columns": columns,
        "row_count": total,
        "sample_rows": sample_rows,
    }


def import_records(
    api_key: str,
    base_id: str,
    table_name: str,
    fields: list[str] | None = None,
) -> list[dict]:
    """Import all records from an Airtable table.

    Args:
        api_key: Airtable personal access token
        base_id: Airtable base ID (starts with "app")
        table_name: Table name or ID
        fields: Optional list of field names to fetch (fetches all if None)

    Returns:
        List of row dicts. Each row has the Airtable field values plus
        "_airtable_record_id" for write-back. Values are stringified for
        compatibility with the schema detector.
    """
    records = _fetch_all_records(api_key, base_id, table_name, fields=fields)

    # Stringify values for schema detector compatibility (it expects str values)
    rows: list[dict] = []
    for rec in records:
        row: dict[str, str] = {}
        for key, value in rec.items():
            if isinstance(value, list):
                row[key] = ", ".join(str(v) for v in value)
            elif value is None:
                row[key] = ""
            else:
                row[key] = str(value)
        rows.append(row)

    return rows


def writeback(
    api_key: str,
    base_id: str,
    table_name: str,
    profiles: list,
    record_id_field: str = "_airtable_record_id",
) -> dict[str, int]:
    """Write enrichment results back to Airtable as new columns.

    For each profile that has an Airtable record ID (stored during import),
    writes PS_* columns with enrichment data. Does NOT overwrite existing
    Airtable data — only adds/updates PS_ prefixed columns.

    Args:
        api_key: Airtable personal access token
        base_id: Airtable base ID
        table_name: Table name or ID
        profiles: List of Profile objects with enrichment data
        record_id_field: Key in profile.metadata holding the Airtable record ID

    Returns:
        {"updated": N, "failed": N, "skipped": N}
    """
    stats = {"updated": 0, "failed": 0, "skipped": 0}

    # Build update batches (Airtable allows up to 10 records per PATCH)
    batch: list[dict] = []

    for profile in profiles:
        record_id = profile.metadata.get(record_id_field, "")
        if not record_id:
            stats["skipped"] += 1
            continue

        # Build the PS_ fields
        fields: dict[str, str] = {}

        if profile.linkedin_url:
            fields["PS_LinkedIn_URL"] = profile.linkedin_url

        if profile.linkedin_enriched:
            # Truncate to Airtable's long text limit (~100K chars)
            context = profile.linkedin_enriched.get("context_block", "")
            if context:
                fields["PS_LinkedIn_Enriched"] = context[:100_000]

        if profile.profile_card:
            fields["PS_Profile_Card"] = profile.profile_card[:100_000]

        fields["PS_Enrichment_Status"] = profile.enrichment_status.value

        from datetime import datetime, timezone

        fields["PS_Last_Enriched"] = datetime.now(timezone.utc).isoformat()

        if not fields:
            stats["skipped"] += 1
            continue

        batch.append({"id": record_id, "fields": fields})

        # Airtable PATCH accepts up to 10 records at a time
        if len(batch) >= 10:
            _flush_batch(api_key, base_id, table_name, batch, stats)
            batch = []
            time.sleep(REQUEST_DELAY)

    # Flush remaining
    if batch:
        _flush_batch(api_key, base_id, table_name, batch, stats)

    return stats


def _flush_batch(
    api_key: str,
    base_id: str,
    table_name: str,
    batch: list[dict],
    stats: dict[str, int],
) -> None:
    """PATCH a batch of up to 10 records to Airtable."""
    try:
        resp = requests.patch(
            f"{AIRTABLE_API}/{base_id}/{table_name}",
            headers=_headers(api_key),
            json={"records": batch},
            timeout=30,
        )
        if resp.status_code == 200:
            stats["updated"] += len(batch)
        else:
            stats["failed"] += len(batch)
    except requests.RequestException:
        stats["failed"] += len(batch)
