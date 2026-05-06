"""GET /api/search/searches/:id/results — Get scored results for a search."""

import re
from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage
from api.search._search_helpers import get_v2_profiles


_URL_RE = re.compile(r"(?:https?://|www\.)[^\s,;|<>\"'\\]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:)]}>\""

# Hosts that are noise as "personal links" — don't surface as badges.
_SKIP_HOSTS = {
    "google.com", "docs.google.com", "drive.google.com",
    "youtube.com", "youtu.be",
    "facebook.com", "instagram.com",
    "wikipedia.org",
}


def _harvest_urls(profile, primary_linkedin: str) -> list[str]:
    """Pull distinct URLs out of identity + content fields. Caps at 8."""
    out: list[str] = []
    seen: set[str] = set()

    def push(u: str) -> None:
        if not u:
            return
        u = u.strip().rstrip(_TRAILING_PUNCT)
        if not u:
            return
        if not u.lower().startswith("http"):
            u = "https://" + u.lstrip("/")
        key = u.lower()
        if key in seen:
            return
        seen.add(key)
        host = re.sub(r"^https?://(?:www\.)?", "", u, flags=re.I).split("/", 1)[0].lower()
        if host in _SKIP_HOSTS:
            return
        out.append(u)

    push(primary_linkedin)
    if profile and getattr(profile, "fields", None):
        for fname, fval in profile.fields.items():
            value = getattr(fval, "value", None) or (fval.get("value") if isinstance(fval, dict) else "")
            if not value:
                continue
            for m in _URL_RE.finditer(str(value)):
                push(m.group(0))
                if len(out) >= 8:
                    return out
    return out


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
                # extra_links populated below for the visible top-N only.
                "extra_links": [],
                "_profile": p,
            }
            if person_id:
                person_seen[person_id] = len(results)
            results.append(row)

        # URL harvesting is the slow path (regex over every field of every
        # profile). The frontend only renders the top 50, so harvest just
        # those — saves seconds on accounts with thousands of scored rows.
        for row in results[:50]:
            p = row.pop("_profile", None)
            if p is not None:
                row["extra_links"] = _harvest_urls(p, row.get("linkedin_url", ""))
        for row in results[50:]:
            row.pop("_profile", None)

        json_response(self, 200, {
            "results": results,
            "excluded_count": excluded_count,
            "search_rules": search.search_rules,
            "feedback_count": len(search.feedback_log),
            "name": search.name,
        })

    def log_message(self, format, *args):
        pass
