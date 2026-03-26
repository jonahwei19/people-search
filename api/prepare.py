"""POST /api/prepare — Parse file with confirmed mappings, return cost estimate."""

import json
from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, parse_multipart, save_temp_file,
    get_pipeline, get_storage,
)
from enrichment.schema import FieldMapping, FieldType
from enrichment.enrichers import is_valid_linkedin_url


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        fields, files = parse_multipart(self)
        if "file" not in files:
            json_response(self, 400, {"error": "No file provided"})
            return

        filename, data = files["file"]
        filepath = save_temp_file(filename, data)
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
            dataset, cost = pipeline.prepare(filepath, fmaps, name=name)

            # Save to Supabase
            storage = get_storage(account["account_id"])
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
