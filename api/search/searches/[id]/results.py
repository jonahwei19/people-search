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
        excluded = set(search.excluded_profile_ids)

        # Phase 1 profile merge: dedupe by person_id across datasets.
        # For each canonical person, keep the highest-scoring row and note
        # how many source-dataset rows exist for them.
        results = []
        excluded_count = 0
        person_seen: dict[str, int] = {}  # person_id -> index in results
        for pid, score in sorted(search.cache.scores.items(), key=lambda x: -x[1].score):
            if pid in excluded:
                excluded_count += 1
                continue
            p = profile_map.get(pid)
            person_id = getattr(p, "person_id", "") if p else ""
            if person_id and person_id in person_seen:
                # Already have a higher-scored row for this person. Count it
                # for the "appears in N datasets" badge.
                results[person_seen[person_id]]["dataset_rows"] += 1
                continue
            row = {
                "id": pid,
                "person_id": person_id,
                "dataset_rows": 1,
                "name": p.identity.name if p else "Unknown",
                "score": score.score,
                "reasoning": score.reasoning,
                "raw_text_preview": (p.raw_text[:400] if p else ""),
                "linkedin_url": (p.identity.linkedin_url if p else ""),
                "email": (p.identity.email if p else ""),
            }
            if person_id:
                person_seen[person_id] = len(results)
            results.append(row)

        json_response(self, 200, {
            "results": results,
            "excluded_count": excluded_count,
            "search_rules": search.search_rules,
            "feedback_count": len(search.feedback_log),
            "name": search.name,
        })

    def log_message(self, format, *args):
        pass
