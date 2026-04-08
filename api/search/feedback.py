"""POST /api/search/feedback — Submit feedback on a search result.

Mirrors local/search_blueprint.py feedback logic:
- Auto-infers rejection reasons for unexplained rejects
- Creates negative exemplars (score=5) for strong_no
- Creates positive exemplars (score=95) for strong_yes
- Extracts prompt corrections when user corrects judge reasoning
- Proposes global rules when scope=global
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from api.search._search_helpers import get_v2_profile
from search.models import FeedbackEvent, Exemplar, GlobalRules
from search.feedback import (
    classify_feedback,
    create_negative_exemplar,
    extract_positive_signal,
    infer_rejection_reason,
    propose_global_rule,
)

MAX_EXEMPLARS = 10


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

        rating = body.get("rating", "no")
        reason = (body.get("reason") or "").strip()
        profile_id = body.get("profile_id", "")

        # Load the profile for classification/exemplar creation
        profile = get_v2_profile(storage, profile_id)

        # Auto-infer reason for rejections without explanation
        inferred = None
        if rating in ("strong_no", "no") and not reason and profile:
            try:
                inferred = infer_rejection_reason(search, profile)
                reason = inferred.get("reason", "Rejected without reason")
            except Exception:
                reason = "Rejected without reason"

        event = FeedbackEvent(
            profile_id=profile_id,
            profile_name=body.get("profile_name", ""),
            rating=rating,
            reason=reason,
            reasoning_correction=body.get("reasoning_correction"),
            scope=body.get("scope", "search"),
        )

        # Store feedback
        storage.add_feedback(search_id, event)

        # Classify the feedback and extract signals
        classification = None
        if profile:
            try:
                classification = classify_feedback(search, event, profile)
            except Exception:
                classification = {"category": "profile", "key_signal": reason or "unknown"}

            # Create negative exemplar for strong rejects + auto-exclude
            if rating == "strong_no":
                create_negative_exemplar(search, profile, reason)
                if profile_id not in search.excluded_profile_ids:
                    search.excluded_profile_ids.append(profile_id)

            # Regular "no" — create weaker negative exemplar (score=15)
            if rating == "no" and profile:
                create_negative_exemplar(search, profile, reason or "Rejected by user")

            # Create positive exemplar for strong accepts
            if rating == "strong_yes":
                try:
                    positive = extract_positive_signal(search, profile)
                    exemplar_reason = positive.get("summary", reason or "Marked as excellent")
                except Exception:
                    exemplar_reason = reason or "Marked as excellent"
                search.exemplars = [ex for ex in search.exemplars if ex.profile_id != profile_id]
                if len(search.exemplars) < MAX_EXEMPLARS:
                    search.exemplars.append(Exemplar(
                        profile_id=profile_id,
                        profile_name=event.profile_name,
                        profile_summary=profile.raw_text[:300],
                        score=95,
                        reason=exemplar_reason,
                    ))

            # Extract prompt corrections from reasoning feedback
            if event.reasoning_correction and classification and classification.get("prompt_correction"):
                correction = classification["prompt_correction"]
                if correction and correction not in search.prompt_corrections:
                    search.prompt_corrections.append(correction)

        # Save search with updated exemplars/corrections
        storage.save_search(search)

        # If global scope, propose a global rule
        global_proposal = None
        if body.get("scope") == "global" and reason:
            try:
                rules = storage.load_rules()
                global_proposal = propose_global_rule(event, GlobalRules(rules=rules))
            except Exception:
                pass

        json_response(self, 200, {
            "status": "ok",
            "global_proposal": global_proposal,
            "inferred_reason": inferred,
            "classification": classification,
        })

    def log_message(self, format, *args):
        pass
