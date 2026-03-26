"""POST /api/detect_schema — Upload file and auto-detect column schema."""

from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, parse_multipart, save_temp_file,
    count_rows, get_pipeline,
)


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
