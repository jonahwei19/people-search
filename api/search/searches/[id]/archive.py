"""POST /api/search/searches/:id/archive — Archive or unarchive a search.

Body: { "archived": true } or { "archived": false }  (defaults to true)
"""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        search_id = path_param(self, -2)
        storage = get_storage(account["account_id"])
        search = storage.load_search(search_id, include_scores=False)

        if not search:
            json_response(self, 404, {"error": "not found"})
            return

        body = read_json_body(self)
        archived = body.get("archived", True)
        search.archived_at = datetime.now(timezone.utc) if archived else None
        storage.save_search(search)

        json_response(self, 200, {
            "status": "ok",
            "archived": bool(search.archived_at),
        })

    def log_message(self, format, *args):
        pass
