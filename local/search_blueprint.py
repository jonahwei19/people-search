"""Search Blueprint — plugs into the enrichment web app (upload_web.py).

Adds a Search tab with:
- Search picker (select existing or create new)
- Questioning onboarding (~3 clarifying questions for new searches)
- LLM judge scoring (Gemini 3.1 Flash-Lite)
- Feedback with rating + reason + scope toggle
- Rule synthesis on re-run
- Global rule injection with relevance pre-filter
"""

import json
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request

from search.feedback import (
    apply_synthesis, propose_global_rule, synthesize_rules,
    infer_rejection_reason, create_negative_exemplar,
    classify_feedback, extract_positive_signal,
)
from search.global_filter import filter_global_rules
from search.llm_judge import rank_results, score_profiles_sync
from search.models import (
    DefinedSearch,
    Exemplar,
    FeedbackEvent,
    GlobalRule,
    GlobalRules,
    Profile as V2Profile,
    ProfileIdentity,
    ScoreResult,
)
from search.feedback import MAX_EXEMPLARS
from search.questioner import conversation_to_context, next_question

search_bp = Blueprint("search", __name__)

SEARCHES_DIR = Path(__file__).parent.parent / "search" / "searches"
SEARCHES_DIR.mkdir(exist_ok=True)
GLOBAL_RULES_PATH = Path(__file__).parent.parent / "search" / "global_rules.json"

SEARCHES: dict[str, DefinedSearch] = {}
GLOBAL_RULES: GlobalRules = GlobalRules()
SCORING_PROGRESS: dict[str, dict] = {}
CONVERSATIONS: dict[str, list[dict]] = {}  # session_id -> [{role, text}]
_pipeline = None


def init_search(pipeline):
    global _pipeline, GLOBAL_RULES, SEARCHES
    _pipeline = pipeline
    GLOBAL_RULES = GlobalRules.load(str(GLOBAL_RULES_PATH))
    for s in DefinedSearch.load_all(str(SEARCHES_DIR)):
        SEARCHES[s.id] = s
    print(f"Search: {len(GLOBAL_RULES.rules)} global rules, {len(SEARCHES)} saved searches")


def _get_v2_profiles(dataset_id: str = None) -> list[V2Profile]:
    """Convert enrichment profiles → v2 profiles for the judge."""
    profiles = []
    for ds_info in _pipeline.list_datasets():
        if dataset_id and ds_info["id"] != dataset_id:
            continue
        ds = _pipeline.load(ds_info["id"])
        if not ds:
            continue
        for p in ds.profiles:
            raw_text = p.profile_card or ""
            if not raw_text:
                parts = []
                if p.linkedin_enriched and p.linkedin_enriched.get("context_block"):
                    parts.append(p.linkedin_enriched["context_block"])
                for name, text in p.content_fields.items():
                    if text and text.strip():
                        parts.append(f"{name}: {text.strip()}")
                raw_text = "\n\n".join(parts)

            profiles.append(V2Profile(
                id=p.id,
                dataset_id=ds.id,
                identity=ProfileIdentity(
                    name=p.display_name(),
                    email=p.email or None,
                    linkedin_url=p.linkedin_url or None,
                ),
                raw_text=raw_text[:3000],
            ))
    return profiles


# ── API Routes ──

@search_bp.route("/api/search/searches")
def list_searches():
    return jsonify({"searches": [
        {
            "id": s.id, "name": s.name, "query": s.query,
            "feedback_count": len(s.feedback_log),
            "rule_count": len(s.search_rules),
            "has_results": bool(s.cache.scores),
        }
        for s in SEARCHES.values()
    ]})


@search_bp.route("/api/search/searches/<search_id>")
def get_search(search_id):
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    return jsonify(s.model_dump(mode="json"))


@search_bp.route("/api/search/datasets")
def search_datasets():
    return jsonify({"datasets": _pipeline.list_datasets()})


@search_bp.route("/api/search/chat", methods=["POST"])
def chat_question():
    """Multi-turn questioning. Send query + conversation so far, get next question or done signal."""
    data = request.json
    query = data.get("query", "")
    session_id = data.get("session_id", "")
    user_answer = data.get("answer")  # None on first call, string on subsequent
    search_id = data.get("search_id")
    existing = SEARCHES.get(search_id) if search_id else None

    # Get or create conversation
    if session_id not in CONVERSATIONS:
        CONVERSATIONS[session_id] = []
    convo = CONVERSATIONS[session_id]

    # Add user's answer to conversation (if not first call)
    if user_answer is not None:
        convo.append({"role": "user", "text": user_answer})


    try:
        result = next_question(query, convo, GLOBAL_RULES.rules, existing)

        if result.get("done"):
            context = conversation_to_context(convo, result.get("summary", ""))
            return jsonify({"done": True, "summary": result.get("summary", ""), "context": context})
        else:
            convo.append({"role": "assistant", "text": result["question"]})
            return jsonify({"done": False, "question": result["question"]})
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"done": True, "summary": query, "context": query})


@search_bp.route("/api/search/score", methods=["POST"])
def start_scoring():
    data = request.json
    name = data.get("name", "Untitled")
    query = data.get("query", "")
    search_id = data.get("search_id")
    dataset_id = data.get("dataset_id")
    clarification = data.get("clarification_context", "")

    if search_id and search_id in SEARCHES:
        search = SEARCHES[search_id]
        search.query = query
        search.name = name
        if clarification:
            search.clarification_context = clarification
    else:
        search = DefinedSearch(name=name, query=query, clarification_context=clarification)
        SEARCHES[search.id] = search
        search_id = search.id

    profiles = _get_v2_profiles(dataset_id)
    if not profiles:
        return jsonify({"error": "No profiles found. Upload a dataset first."}), 400

    # Filter global rules for relevance
    try:
        applicable = filter_global_rules(search, GLOBAL_RULES.rules)
    except Exception:
        applicable = GLOBAL_RULES.rules

    SCORING_PROGRESS[search_id] = {"done": 0, "total": len(profiles), "status": "running"}

    def do_score():
        def progress_cb(done, total):
            SCORING_PROGRESS[search_id] = {"done": done, "total": total, "status": "running"}

        scores = score_profiles_sync(search, profiles, applicable, progress_cb)
        search.cache.scores = scores
        search.cache.prompt_hash = search.compute_prompt_hash(GLOBAL_RULES.rules)
        search.save(str(SEARCHES_DIR))
        SCORING_PROGRESS[search_id] = {"done": len(profiles), "total": len(profiles), "status": "done"}

    threading.Thread(target=do_score).start()
    return jsonify({"search_id": search_id, "profile_count": len(profiles)})


@search_bp.route("/api/search/progress/<search_id>")
def get_progress(search_id):
    return jsonify(SCORING_PROGRESS.get(search_id, {"done": 0, "total": 0, "status": "unknown"}))


@search_bp.route("/api/search/searches/<search_id>/results")
def get_results(search_id):
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404

    profiles = _get_v2_profiles()
    profile_map = {p.id: p for p in profiles}

    excluded = set(s.excluded_profile_ids)
    results = []
    excluded_count = 0
    for pid, score in sorted(s.cache.scores.items(), key=lambda x: -x[1].score):
        if pid in excluded:
            excluded_count += 1
            continue
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
    return jsonify({"results": results, "excluded_count": excluded_count})


@search_bp.route("/api/search/feedback", methods=["POST"])
def submit_feedback():
    data = request.json
    search_id = data.get("search_id")
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404

    rating = data.get("rating", "no")
    reason = data.get("reason", "").strip()
    profile_id = data.get("profile_id", "")

    # Auto-infer reason for strong rejections without explanation
    inferred = None
    if rating in ("strong_no", "no") and not reason:
        profiles = _get_v2_profiles()
        profile = next((p for p in profiles if p.id == profile_id), None)
        if profile:
            try:
                inferred = infer_rejection_reason(s, profile)
                reason = inferred.get("reason", "Rejected without reason")
            except Exception:
                reason = "Rejected without reason"

    event = FeedbackEvent(
        profile_id=profile_id,
        profile_name=data.get("profile_name", ""),
        rating=rating,
        reason=reason,
        reasoning_correction=data.get("reasoning_correction"),
        scope=data.get("scope", "search"),
    )
    s.feedback_log.append(event)

    profiles = _get_v2_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)

    # Classify the feedback and extract signals
    classification = None
    if profile:
        try:
            classification = classify_feedback(s, event, profile)
        except Exception:
            classification = {"category": "profile", "key_signal": reason or "unknown"}

        # Handle based on classification
        if rating == "strong_no":
            create_negative_exemplar(s, profile, reason)

        if rating == "strong_yes":
            # Extract what makes this profile great → positive exemplar
            try:
                positive = extract_positive_signal(s, profile)
                exemplar_reason = positive.get("summary", reason or "Marked as excellent")
            except Exception:
                exemplar_reason = reason or "Marked as excellent"
            # Remove existing exemplar for this profile
            s.exemplars = [ex for ex in s.exemplars if ex.profile_id != profile_id]
            if len(s.exemplars) < MAX_EXEMPLARS:
                s.exemplars.append(Exemplar(
                    profile_id=profile_id,
                    profile_name=event.profile_name,
                    profile_summary=profile.raw_text[:300],
                    score=95,
                    reason=exemplar_reason,
                ))

        # If user corrected the judge's reasoning → create prompt correction
        if event.reasoning_correction and classification and classification.get("prompt_correction"):
            correction = classification["prompt_correction"]
            if correction and correction not in s.prompt_corrections:
                s.prompt_corrections.append(correction)

    # If global scope, propose a global rule
    global_proposal = None
    if data.get("scope") == "global" and reason:
        try:
            global_proposal = propose_global_rule(event, GLOBAL_RULES)
        except Exception:
            pass

    s.save(str(SEARCHES_DIR))
    return jsonify({
        "status": "ok",
        "global_proposal": global_proposal,
        "inferred_reason": inferred,
        "classification": classification,
    })


@search_bp.route("/api/search/searches/<search_id>/synthesize", methods=["POST"])
def synthesize_search_rules(search_id):
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404

    profiles = _get_v2_profiles()
    try:
        proposal = synthesize_rules(s, profiles)
        return jsonify({"proposal": proposal})
    except Exception as e:
        return jsonify({"proposal": {"new_rules": [], "modified_rules": [], "add_exemplars": [], "remove_exemplar_ids": [], "notes": str(e)}})


@search_bp.route("/api/search/searches/<search_id>/apply-proposal", methods=["POST"])
def apply_search_proposal(search_id):
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404

    proposal = request.json.get("proposal", {})
    profiles = _get_v2_profiles()
    apply_synthesis(s, proposal, profiles)
    s.save(str(SEARCHES_DIR))
    return jsonify({"status": "ok"})


@search_bp.route("/api/search/searches/<search_id>/rerun", methods=["POST"])
def rerun_search(search_id):
    """Synthesize feedback, then re-score."""
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404

    profiles = _get_v2_profiles()

    try:
        applicable = filter_global_rules(s, GLOBAL_RULES.rules)
    except Exception:
        applicable = GLOBAL_RULES.rules

    SCORING_PROGRESS[search_id] = {"done": 0, "total": len(profiles), "status": "running"}

    def do_score():
        def progress_cb(done, total):
            SCORING_PROGRESS[search_id] = {"done": done, "total": total, "status": "running"}

        scores = score_profiles_sync(s, profiles, applicable, progress_cb)
        s.cache.scores = scores
        s.cache.prompt_hash = s.compute_prompt_hash(GLOBAL_RULES.rules)
        s.save(str(SEARCHES_DIR))
        SCORING_PROGRESS[search_id] = {"done": len(profiles), "total": len(profiles), "status": "done"}

    threading.Thread(target=do_score).start()
    return jsonify({"status": "scoring", "profile_count": len(profiles)})


@search_bp.route("/api/search/searches/<search_id>/rename", methods=["POST"])
def rename_search(search_id):
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    s.name = request.json.get("name", s.name)
    s.save(str(SEARCHES_DIR))
    return jsonify({"status": "ok"})


@search_bp.route("/api/search/searches/<search_id>/exclude", methods=["POST"])
def exclude_profile(search_id):
    """Hide a profile from search results without negative feedback."""
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    pid = request.json.get("profile_id", "")
    if pid and pid not in s.excluded_profile_ids:
        s.excluded_profile_ids.append(pid)
        s.save(str(SEARCHES_DIR))
    return jsonify({"status": "ok"})


@search_bp.route("/api/search/searches/<search_id>/unexclude", methods=["POST"])
def unexclude_profile(search_id):
    """Unhide a profile from search results."""
    s = SEARCHES.get(search_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    pid = request.json.get("profile_id", "")
    if pid in s.excluded_profile_ids:
        s.excluded_profile_ids.remove(pid)
        s.save(str(SEARCHES_DIR))
    return jsonify({"status": "ok"})


@search_bp.route("/api/search/import", methods=["POST"])
def import_search():
    """Import a search from JSON (exported from another instance)."""
    data = request.json
    try:
        search = DefinedSearch.model_validate(data)
        # Generate new ID to avoid collisions
        import uuid
        search.id = str(uuid.uuid4())[:8]
        SEARCHES[search.id] = search
        search.save(str(SEARCHES_DIR))
        return jsonify({"status": "ok", "search_id": search.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@search_bp.route("/api/search/global-rules")
def list_global_rules():
    return jsonify({"rules": [r.model_dump(mode="json") for r in GLOBAL_RULES.rules]})


@search_bp.route("/api/search/global-rules", methods=["POST"])
def add_global_rule():
    data = request.json
    rule = GlobalRule(
        text=data.get("text", ""),
        scope=data.get("scope", "all searches"),
        source="manual",
    )
    GLOBAL_RULES.rules.append(rule)
    GLOBAL_RULES.save(str(GLOBAL_RULES_PATH))
    return jsonify({"status": "ok", "rule_id": rule.id})


# These exports are no longer used (UI is inline in upload_web.py) but kept for reference
SEARCH_NAV = ""
SEARCH_PAGE = ""
SEARCH_JS = ""
