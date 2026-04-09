"""POST /api/search/import_search — Import a search from JSON."""

import uuid
from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from search.models import DefinedSearch


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        try:
            search = DefinedSearch.model_validate(body)
            search.id = str(uuid.uuid4())[:8]  # new ID to avoid collisions
            storage = get_storage(account["account_id"])
            storage.save_search(search)
            json_response(self, 200, {"status": "ok", "search_id": search.id})
        except Exception as e:
            json_response(self, 400, {"error": str(e)})

    def log_message(self, format, *args):
        pass
