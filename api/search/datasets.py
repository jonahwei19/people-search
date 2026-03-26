"""GET /api/search/datasets — List datasets (for search UI dataset picker)."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return
        storage = get_storage(account["account_id"])
        json_response(self, 200, {"datasets": storage.list_datasets()})

    def log_message(self, format, *args):
        pass
