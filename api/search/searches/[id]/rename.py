"""POST /api/search/searches/:id/rename — Rename a search."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        search_id = path_param(self, -2)
        storage = get_storage(account["account_id"])
        search = storage.load_search(search_id)

        if not search:
            json_response(self, 404, {"error": "not found"})
            return

        body = read_json_body(self)
        search.name = body.get("name", search.name)
        storage.save_search(search)

        json_response(self, 200, {"status": "ok"})

    def log_message(self, format, *args):
        pass
