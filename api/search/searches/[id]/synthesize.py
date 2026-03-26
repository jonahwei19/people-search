"""POST /api/search/searches/:id/synthesize — Synthesize rules from feedback."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage
from api.search._search_helpers import get_v2_profiles
from search.feedback import synthesize_rules


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

        profiles = get_v2_profiles(storage)
        try:
            proposal = synthesize_rules(search, profiles)
            json_response(self, 200, {"proposal": proposal})
        except Exception as e:
            json_response(self, 200, {
                "proposal": {
                    "new_rules": [], "modified_rules": [],
                    "add_exemplars": [], "remove_exemplar_ids": [],
                    "notes": str(e),
                }
            })

    def log_message(self, format, *args):
        pass
