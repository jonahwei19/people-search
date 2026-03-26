"""POST /api/detect_schema — Detect column schema from uploaded file content."""

from http.server import BaseHTTPRequestHandler
from pathlib import Path
import tempfile

from api._helpers import (
    require_auth, json_response, read_json_body, parse_multipart, save_temp_file,
    count_rows, get_pipeline,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        content_type = self.headers.get("Content-Type", "")

        # Support both JSON body (large files) and multipart FormData (small files)
        if "application/json" in content_type:
            body = read_json_body(self)
            if not body or not body.get("content"):
                json_response(self, 400, {"error": "No file content provided"})
                return
            filename = body.get("filename", "upload.csv")
            suffix = ".json" if filename.endswith(".json") else ".csv"
            tmp = Path(tempfile.mktemp(suffix=suffix))
            tmp.write_text(body["content"], encoding="utf-8")
            filepath = tmp
        else:
            fields, files = parse_multipart(self)
            if "file" not in files:
                json_response(self, 400, {"error": "No file provided"})
                return
            fname, data = files["file"]
            filepath = save_temp_file(fname, data)

        try:
            pipeline = get_pipeline(account["account_id"])
            mappings = pipeline.detect_schema(filepath)
            json_response(self, 200, {
                "mappings": [m.to_dict() for m in mappings],
                "filename": filepath.name,
                "rows": count_rows(filepath),
            })
        except Exception as e:
            json_response(self, 400, {"error": str(e)})
        finally:
            filepath.unlink(missing_ok=True)

    def log_message(self, format, *args):
        pass
