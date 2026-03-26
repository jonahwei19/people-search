"""POST /api/search/score — Start scoring profiles against a search query.

Runs synchronously (up to 900s). Updates search cache when done.
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from api.search._search_helpers import get_v2_profiles
from search.models import DefinedSearch
from search.llm_judge import score_profiles_sync
from search.global_filter import filter_global_rules


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        name = body.get("name", "Untitled")
        query = body.get("query", "")
        search_id = body.get("search_id")
        dataset_id = body.get("dataset_id")
        clarification = body.get("clarification_context", "")

        storage = get_storage(account["account_id"])

        # Load or create search
        if search_id:
            search = storage.load_search(search_id)
            if search:
                search.query = query
                search.name = name
                if clarification:
                    search.clarification_context = clarification
            else:
                search = DefinedSearch(name=name, query=query,
                                       clarification_context=clarification)
        else:
            search = DefinedSearch(name=name, query=query,
                                   clarification_context=clarification)

        profiles = get_v2_profiles(storage, dataset_id)
        if not profiles:
            json_response(self, 400, {"error": "No profiles found. Upload a dataset first."})
            return

        # Filter global rules
        global_rules = storage.load_rules()
        try:
            applicable = filter_global_rules(search, global_rules)
        except Exception:
            applicable = global_rules

        # Score (synchronous)
        try:
            scores = score_profiles_sync(search, profiles, applicable)
            search.cache.scores = scores
            search.cache.prompt_hash = search.compute_prompt_hash(global_rules)
            storage.save_search(search)

            json_response(self, 200, {
                "search_id": search.id,
                "profile_count": len(profiles),
                "status": "done",
            })
        except Exception as e:
            json_response(self, 500, {"error": str(e)})

    def log_message(self, format, *args):
        pass
