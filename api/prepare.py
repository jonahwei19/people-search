"""POST /api/prepare — Parse file with confirmed mappings, return cost estimate."""

import json
import tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api._helpers import (
    require_auth, json_response, read_json_body, parse_multipart, save_temp_file,
    get_pipeline, get_storage,
)
from enrichment.schema import FieldMapping, FieldType
from enrichment.enrichers import is_valid_linkedin_url


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        content_type = self.headers.get("Content-Type", "")

        # Support both JSON body (large files) and multipart FormData
        if "application/json" in content_type:
            body = read_json_body(self)
            if not body or not body.get("content"):
                json_response(self, 400, {"error": "No file content provided"})
                return
            filename = body.get("filename", "upload.csv")
            suffix = ".json" if filename.endswith(".json") else ".csv"
            filepath = Path(tempfile.mktemp(suffix=suffix))
            filepath.write_text(body["content"], encoding="utf-8")
            name = body.get("name", "")
            raw_mappings = body.get("mappings", [])
        else:
            fields, files = parse_multipart(self)
            if "file" not in files:
                json_response(self, 400, {"error": "No file provided"})
                return
            fname, data = files["file"]
            filepath = save_temp_file(fname, data)
            name = fields.get("name", "")
            raw_mappings = json.loads(fields.get("mappings", "[]"))

        try:
            fmaps = [
                FieldMapping(
                    source_column=m["source_column"],
                    field_type=FieldType(m["field_type"]),
                    target_name=m.get("target_name", m["source_column"]),
                    sample_values=m.get("sample_values", []),
                    confidence=m.get("confidence", 0.5),
                )
                for m in raw_mappings
            ]

            pipeline = get_pipeline(account["account_id"])
            storage = get_storage(account["account_id"])

            # Support appending to existing dataset (for chunked uploads)
            append_to = body.get("append_to") if "application/json" in content_type else None
            if append_to:
                existing = storage.load_dataset(append_to)
                chunk_ds, _ = pipeline.prepare(filepath, fmaps, name=name)
                existing.profiles.extend(chunk_ds.profiles)
                existing.total_rows += chunk_ds.total_rows
                storage.save_dataset(existing)
                storage.save_profiles(append_to, chunk_ds.profiles)
                json_response(self, 200, {"dataset_id": append_to, "appended": len(chunk_ds.profiles)})
                filepath.unlink(missing_ok=True)
                return

            dataset, cost = pipeline.prepare(filepath, fmaps, name=name)
            storage.save_dataset(dataset)

            have_li = sum(1 for p in dataset.profiles if is_valid_linkedin_url(p.linkedin_url))
            email_only = sum(
                1 for p in dataset.profiles
                if p.email and not is_valid_linkedin_url(p.linkedin_url)
            )
            cfields = set()
            for p in dataset.profiles:
                cfields.update(p.content_fields.keys())

            json_response(self, 200, {
                "dataset_id": dataset.id,
                "stats": {
                    "total": len(dataset.profiles),
                    "have_linkedin": have_li,
                    "email_only": email_only,
                    "content_fields": len(cfields),
                    "content_field_names": sorted(cfields),
                    "skipped_rows": dataset.enrichment_stats.get("skipped_rows", 0),
                    "duplicates": len(dataset.enrichment_stats.get("duplicates", [])),
                },
                "cost_summary": cost.summary(),
                "cost": cost.to_dict(),
            })
        except Exception as e:
            json_response(self, 400, {"error": str(e)})
        finally:
            filepath.unlink(missing_ok=True)

    def log_message(self, format, *args):
        pass
