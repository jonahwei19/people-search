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

        # Send immediate response with search_id, then score in background
        # (Vercel functions are synchronous, but we can at least respond quickly
        # for the "creating search" part and score after)

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

        # Save search first so frontend can track it
        storage.save_search(search)

        # Score synchronously — frontend polls /api/search/progress/:id
        # Update job progress as we go
        job_id = storage.create_job(search.id, len(profiles))

        try:
            def on_progress(done, total):
                storage.update_job(job_id, current_count=done,
                                   message=f"Scoring {done}/{total}")

            scores = score_profiles_sync(search, profiles, applicable, on_progress)
            search.cache.scores = scores
            search.cache.prompt_hash = search.compute_prompt_hash(global_rules)
            storage.save_search(search)
            storage.update_job(job_id, status="done", current_count=len(profiles))

            json_response(self, 200, {
                "search_id": search.id,
                "job_id": job_id,
                "profile_count": len(profiles),
                "status": "done",
            })
        except Exception as e:
            storage.update_job(job_id, status="error", message=str(e))
            json_response(self, 500, {"error": str(e)})

    def log_message(self, format, *args):
        pass
