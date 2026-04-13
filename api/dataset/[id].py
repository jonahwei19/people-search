"""GET/POST/DELETE /api/dataset/:id — Dataset detail, update, or delete."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, read_json_body, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        ds_id = path_param(self)
        storage = get_storage(account["account_id"])

        try:
            ds = storage.load_dataset(ds_id)
            json_response(self, 200, {
                "id": ds.id,
                "name": ds.name,
                "created_at": ds.created_at,
                "source_file": ds.source_file,
                "total_rows": ds.total_rows,
                "searchable_fields": ds.searchable_fields,
                "enrichment_stats": ds.enrichment_stats,
                "field_mappings": ds.field_mappings,
                "profiles": [p.to_dict() for p in ds.profiles],
            })
        except Exception:
            json_response(self, 404, {"error": "Dataset not found"})

    def do_POST(self):
        """Update dataset metadata (e.g., field_mappings)."""
        account = require_auth(self)
        if not account:
            return

        ds_id = path_param(self)
        body = read_json_body(self)
        storage = get_storage(account["account_id"])

        try:
            ds = storage.load_dataset(ds_id)
            if "field_mappings" in body:
                ds.field_mappings = body["field_mappings"]
            # Update metadata only (skip re-saving all profiles)
            storage.client.table("datasets").update({
                "field_mappings": ds.field_mappings,
            }).eq("id", ds_id).eq("account_id", account["account_id"]).execute()
            json_response(self, 200, {"status": "ok"})
        except Exception as e:
            json_response(self, 400, {"error": str(e)})

    def do_DELETE(self):
        account = require_auth(self)
        if not account:
            return

        ds_id = path_param(self)
        storage = get_storage(account["account_id"])

        try:
            storage.delete_dataset(ds_id)
            json_response(self, 200, {"ok": True})
        except Exception as e:
            json_response(self, 400, {"error": str(e)})

    def log_message(self, format, *args):
        pass
