"""Airtable integration API routes for Vercel Python runtime.

Three endpoints:
    POST /api/airtable/connect    — preview table columns + sample rows
    POST /api/airtable/import     — import records into a new dataset
    POST /api/airtable/writeback  — push enrichment results back to Airtable

All endpoints require a valid session cookie (account_id).
Airtable API key comes from the request body (not stored server-side —
each org manages their own key).
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from enrichment.airtable import connect, import_records, writeback, AirtableError
from enrichment.schema import SchemaDetector
from enrichment.pipeline import EnrichmentPipeline
from enrichment.models import Dataset, Profile
from cloud.storage import SupabaseStorage


def _get_storage(account_id: str) -> SupabaseStorage:
    return SupabaseStorage(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_key=os.environ["SUPABASE_SERVICE_KEY"],
        account_id=account_id,
    )


def _get_account_id(headers: dict) -> str | None:
    """Extract account_id from session cookie.

    In production this reads a signed HTTP-only cookie set by the auth
    routes. For now, also accept an X-Account-ID header for testing.
    """
    # TODO: replace with proper cookie/session validation from auth module
    return headers.get("x-account-id") or headers.get("X-Account-Id")


def _error(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "body": json.dumps({"error": message}),
        "headers": {"Content-Type": "application/json"},
    }


def _ok(data: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "body": json.dumps(data),
        "headers": {"Content-Type": "application/json"},
    }


# ── Vercel handler ────────────────────────────────────────────


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime handler for /api/airtable.

    Routes by the last path segment:
        /api/airtable/connect   → _handle_connect
        /api/airtable/import    → _handle_import
        /api/airtable/writeback → _handle_writeback
    """

    def do_POST(self):
        account_id = _get_account_id(dict(self.headers))
        if not account_id:
            self._respond(401, {"error": "Not authenticated"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}
        except (json.JSONDecodeError, ValueError):
            self._respond(400, {"error": "Invalid JSON body"})
            return

        # Route by path suffix
        path = self.path.rstrip("/").split("/")[-1]
        handlers = {
            "connect": self._handle_connect,
            "import": self._handle_import,
            "writeback": self._handle_writeback,
        }

        fn = handlers.get(path)
        if not fn:
            self._respond(404, {"error": f"Unknown action: {path}"})
            return

        fn(account_id, body)

    def _handle_connect(self, account_id: str, body: dict):
        """POST /api/airtable/connect

        Body: { "api_key": "...", "base_id": "...", "table_name": "..." }
        Returns: { "columns": [...], "row_count": N, "sample_rows": [...] }
        """
        api_key = body.get("api_key", "")
        base_id = body.get("base_id", "")
        table_name = body.get("table_name", "")

        if not all([api_key, base_id, table_name]):
            self._respond(400, {"error": "api_key, base_id, and table_name are required"})
            return

        try:
            info = connect(api_key, base_id, table_name)
        except AirtableError as e:
            self._respond(e.status_code, {"error": str(e)})
            return

        # Run schema detection on sample rows so the frontend can show mappings
        if info["sample_rows"]:
            detector = SchemaDetector()
            columns = info["columns"]
            sample_rows = info["sample_rows"]
            # _detect_columns expects list[dict] with string values
            str_rows = [
                {k: str(v) if v is not None else "" for k, v in row.items()}
                for row in sample_rows
            ]
            mappings = detector._detect_columns(columns, str_rows)
            info["suggested_mappings"] = [m.to_dict() for m in mappings]

        self._respond(200, info)

    def _handle_import(self, account_id: str, body: dict):
        """POST /api/airtable/import

        Body: {
            "api_key": "...", "base_id": "...", "table_name": "...",
            "mappings": [...],  // confirmed field mappings from UI
            "dataset_name": "My Import"
        }
        Returns: { "dataset_id": "..." }
        """
        api_key = body.get("api_key", "")
        base_id = body.get("base_id", "")
        table_name = body.get("table_name", "")
        raw_mappings = body.get("mappings", [])
        dataset_name = body.get("dataset_name", f"Airtable: {table_name}")

        if not all([api_key, base_id, table_name]):
            self._respond(400, {"error": "api_key, base_id, and table_name are required"})
            return

        if not raw_mappings:
            self._respond(400, {"error": "mappings are required (run /connect first)"})
            return

        # Fetch all records
        try:
            rows = import_records(api_key, base_id, table_name)
        except AirtableError as e:
            self._respond(e.status_code, {"error": str(e)})
            return

        # Convert mappings from JSON to FieldMapping objects
        from enrichment.schema import FieldMapping, FieldType

        mappings = [
            FieldMapping(
                source_column=m["source_column"],
                field_type=FieldType(m["field_type"]),
                target_name=m.get("target_name", ""),
                sample_values=m.get("sample_values", []),
                confidence=m.get("confidence", 1.0),
            )
            for m in raw_mappings
        ]

        # Build profiles using the pipeline's row_to_profile logic
        pipeline = EnrichmentPipeline()
        profiles: list[Profile] = []
        for i, row in enumerate(rows):
            profile = pipeline._row_to_profile(row, mappings, i)

            # Preserve Airtable record ID for write-back
            airtable_id = row.get("_airtable_record_id", "")
            if airtable_id:
                profile.metadata["_airtable_record_id"] = airtable_id

            # Skip junk rows (no identity and no content)
            has_identity = profile.name or profile.email or profile.linkedin_url
            has_content = any(
                v.strip() for v in profile.content_fields.values()
            ) if profile.content_fields else False
            if not has_identity and not has_content:
                continue

            profiles.append(profile)

        # Build and save dataset
        dataset = Dataset(
            name=dataset_name,
            source_file=f"airtable://{base_id}/{table_name}",
            total_rows=len(rows),
            profiles=profiles,
            field_mappings=[m.to_dict() for m in mappings],
        )

        storage = _get_storage(account_id)
        storage.save_dataset(dataset)

        self._respond(200, {
            "dataset_id": dataset.id,
            "profiles_imported": len(profiles),
            "rows_skipped": len(rows) - len(profiles),
        })

    def _handle_writeback(self, account_id: str, body: dict):
        """POST /api/airtable/writeback

        Body: {
            "dataset_id": "...",
            "api_key": "...", "base_id": "...", "table_name": "..."
        }
        Returns: { "updated": N, "failed": N, "skipped": N }
        """
        dataset_id = body.get("dataset_id", "")
        api_key = body.get("api_key", "")
        base_id = body.get("base_id", "")
        table_name = body.get("table_name", "")

        if not all([dataset_id, api_key, base_id, table_name]):
            self._respond(400, {
                "error": "dataset_id, api_key, base_id, and table_name are required"
            })
            return

        # Load profiles from Supabase
        storage = _get_storage(account_id)
        try:
            dataset = storage.load_dataset(dataset_id)
        except Exception:
            self._respond(404, {"error": f"Dataset {dataset_id} not found"})
            return

        # Write back to Airtable
        try:
            stats = writeback(
                api_key=api_key,
                base_id=base_id,
                table_name=table_name,
                profiles=dataset.profiles,
            )
        except AirtableError as e:
            self._respond(e.status_code, {"error": str(e)})
            return

        self._respond(200, stats)

    def _respond(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
