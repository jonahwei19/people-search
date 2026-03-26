"""POST /api/search/feedback — Submit feedback on a search result."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from search.models import FeedbackEvent
from search.feedback import propose_global_rule
from search.models import GlobalRules


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        search_id = body.get("search_id")
        if not search_id:
            json_response(self, 400, {"error": "search_id required"})
            return

        storage = get_storage(account["account_id"])
        search = storage.load_search(search_id)
        if not search:
            json_response(self, 404, {"error": "not found"})
            return

        event = FeedbackEvent(
            profile_id=body.get("profile_id", ""),
            profile_name=body.get("profile_name", ""),
            rating=body.get("rating", "no"),
            reason=body.get("reason"),
            reasoning_correction=body.get("reasoning_correction"),
            scope=body.get("scope", "search"),
        )

        # Store feedback
        storage.add_feedback(search_id, event)

        # If global scope, propose a global rule
        global_proposal = None
        if body.get("scope") == "global" and body.get("reason"):
            try:
                rules = storage.load_rules()
                global_proposal = propose_global_rule(event, GlobalRules(rules=rules))
            except Exception:
                pass

        json_response(self, 200, {"status": "ok", "global_proposal": global_proposal})

    def log_message(self, format, *args):
        pass
