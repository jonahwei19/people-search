"""GET /api/search/searches/:id — Get search detail (lightweight).

Returns search metadata without the full cache_scores blob.
The frontend uses /api/search/searches/:id/results for actual results.
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
        # Lightweight load — skip cache_scores (frontend uses /results endpoint
        # and already knows has_results from the list endpoint)
        search = storage.load_search(search_id, include_scores=False)

        if not search:
            json_response(self, 404, {"error": "not found"})
            return

        data = search.model_dump(mode="json")
        # Strip cache scores — frontend gets results from /results endpoint
        data.pop("cache", None)
        json_response(self, 200, data)

    def log_message(self, format, *args):
        pass
