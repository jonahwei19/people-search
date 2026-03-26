"""GET /api/search/searches/:id — Get search detail."""

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
            json_response(self, 404, {"error": "not found"})
            return

        json_response(self, 200, search.model_dump(mode="json"))

    def log_message(self, format, *args):
        pass
