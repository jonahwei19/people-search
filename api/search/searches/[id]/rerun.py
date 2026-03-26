"""POST /api/search/searches/:id/rerun — Re-score after feedback (900s timeout)."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage
from api.search._search_helpers import get_v2_profiles
from search.llm_judge import score_profiles_sync
from search.global_filter import filter_global_rules


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
        global_rules = storage.load_rules()

        try:
            applicable = filter_global_rules(search, global_rules)
        except Exception:
            applicable = global_rules

        try:
            scores = score_profiles_sync(search, profiles, applicable)
            search.cache.scores = scores
            search.cache.prompt_hash = search.compute_prompt_hash(global_rules)
            storage.save_search(search)

            json_response(self, 200, {
                "status": "done",
                "profile_count": len(profiles),
            })
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def log_message(self, format, *args):
        pass
