"""GET /api/search/progress/:id — Scoring progress (for polling).

In the cloud version, scoring is synchronous so this just returns "done"
if the search has scores, or "unknown" otherwise.
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        search_id = path_param(self)
        storage = get_storage(account["account_id"])
        search = storage.load_search(search_id)

        if not search:
            json_response(self, 200, {"done": 0, "total": 0, "status": "unknown"})
            return

        total = len(search.cache.scores)
        json_response(self, 200, {
            "done": total,
            "total": total,
            "status": "done" if total > 0 else "unknown",
        })

    def log_message(self, format, *args):
        pass
