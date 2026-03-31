"""GET /api/search/searches/:id/results — Get scored results for a search."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage
from api.search._search_helpers import get_v2_profiles


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        search_id = path_param(self, -2)  # .../searches/{id}/results
        storage = get_storage(account["account_id"])
        search = storage.load_search(search_id)

        if not search:
            json_response(self, 404, {"error": "not found"})
            return

        profiles = get_v2_profiles(storage)
        profile_map = {p.id: p for p in profiles}

        results = []
        for pid, score in sorted(search.cache.scores.items(), key=lambda x: -x[1].score):
            p = profile_map.get(pid)
            results.append({
                "id": pid,
                "name": p.identity.name if p else "Unknown",
                "score": score.score,
                "reasoning": score.reasoning,
                "raw_text_preview": (p.raw_text[:400] if p else ""),
                "linkedin_url": (p.identity.linkedin_url if p else ""),
                "email": (p.identity.email if p else ""),
            })

        json_response(self, 200, {"results": results})

    def log_message(self, format, *args):
        pass
