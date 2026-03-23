"""Feedback system: collects ratings, synthesizes rules, manages exemplars. Uses Gemini."""

from __future__ import annotations

from typing import Optional

from v2.gemini_helpers import call_gemini_json
from v2.models import (
    DefinedSearch,
    Exemplar,
    FeedbackEvent,
    GlobalRule,
    GlobalRules,
    Profile,
    ScoreResult,
)

MAX_EXEMPLARS = 10

SYNTHESIS_SYSTEM = """You are analyzing user feedback on search results to improve future searches.

Given feedback events (accept/reject with reasons), do three things:

1. RULES: Propose 0-2 new search-specific rules if a clear pattern exists across multiple feedbacks. Don't propose a rule from a single event.

2. EXEMPLAR CHANGES: Decide which profiles should become calibration exemplars. Keep total to ~10, ~2 per score level, balanced. Map ratings: strong_yes→95, yes→75, no→25, strong_no→5.

3. CONSOLIDATION: Suggest consolidation if rules/exemplars are redundant.

Respond as JSON:
{
  "new_rules": ["rule text", ...],
  "modified_rules": [{"old": "old text", "new": "new text"}, ...],
  "add_exemplars": [{"profile_id": "id", "profile_name": "name", "score": N, "reason": "why"}],
  "remove_exemplar_ids": ["id", ...],
  "notes": "brief explanation"
}

If no changes are warranted, return empty arrays."""


def synthesize_rules(
    search: DefinedSearch,
    profiles: list[Profile],
    new_feedback: Optional[list[FeedbackEvent]] = None,
) -> dict:
    """Analyze feedback and propose rule/exemplar changes."""
    feedback = new_feedback or search.feedback_log
    if not feedback:
        return {"new_rules": [], "modified_rules": [], "add_exemplars": [], "remove_exemplar_ids": [], "notes": "No feedback."}

    profile_map = {p.id: p for p in profiles}

    parts = [f"SEARCH: {search.name}\nQUERY: {search.query}\n"]

    if search.search_rules:
        parts.append("CURRENT RULES:")
        for r in search.search_rules:
            parts.append(f"- {r}")

    if search.exemplars:
        parts.append(f"\nCURRENT EXEMPLARS ({len(search.exemplars)}):")
        for ex in search.exemplars:
            parts.append(f"- Score {ex.score}: {ex.profile_name} — \"{ex.reason}\"")

    parts.append(f"\nFEEDBACK ({len(feedback)} events):")
    for fb in feedback:
        profile = profile_map.get(fb.profile_id)
        summary = profile.raw_text[:300] if profile else ""
        parts.append(f"\n{fb.rating.upper()}: {fb.profile_name}")
        if fb.reason:
            parts.append(f"  Reason: {fb.reason}")
        if fb.reasoning_correction:
            parts.append(f"  Correction: {fb.reasoning_correction}")
        if summary:
            parts.append(f"  Profile: {summary}")

    try:
        return call_gemini_json(SYNTHESIS_SYSTEM, "\n".join(parts))
    except Exception as e:
        return {"new_rules": [], "modified_rules": [], "add_exemplars": [], "remove_exemplar_ids": [], "notes": f"Error: {e}"}


def apply_synthesis(search: DefinedSearch, proposal: dict, profiles: list[Profile]) -> None:
    """Apply a confirmed synthesis proposal to the search."""
    profile_map = {p.id: p for p in profiles}

    for rule in proposal.get("new_rules", []):
        if rule and rule not in search.search_rules:
            search.search_rules.append(rule)

    for mod in proposal.get("modified_rules", []):
        old, new = mod.get("old", ""), mod.get("new", "")
        if old in search.search_rules:
            idx = search.search_rules.index(old)
            search.search_rules[idx] = new

    remove_ids = set(proposal.get("remove_exemplar_ids", []))
    search.exemplars = [ex for ex in search.exemplars if ex.profile_id not in remove_ids]

    for ex_data in proposal.get("add_exemplars", []):
        if len(search.exemplars) >= MAX_EXEMPLARS:
            break
        pid = ex_data.get("profile_id", "")
        if any(ex.profile_id == pid for ex in search.exemplars):
            continue
        profile = profile_map.get(pid)
        search.exemplars.append(Exemplar(
            profile_id=pid,
            profile_name=ex_data.get("profile_name", ""),
            profile_summary=profile.raw_text[:300] if profile else "",
            score=ex_data.get("score", 50),
            reason=ex_data.get("reason", ""),
        ))


GLOBAL_RULE_CHECK_SYSTEM = """Evaluate if this feedback should become a global rule.

Global rules MUST be:
1. Scoped: "When [condition], [rule]"
2. Not specific to one profile or search
3. Applicable across different searches

Respond as JSON:
{"propose_rule": true/false, "rule_text": "When ..., ...", "scope": "when this fires", "reason": "why"}"""


def propose_global_rule(feedback_event: FeedbackEvent, existing_rules: GlobalRules) -> Optional[dict]:
    """Check if feedback warrants a new global rule."""
    parts = [
        f"FEEDBACK: {feedback_event.rating} on {feedback_event.profile_name}",
        f"Reason: {feedback_event.reason or 'none'}",
    ]
    if existing_rules.rules:
        parts.append("\nEXISTING GLOBAL RULES:")
        for r in existing_rules.rules:
            parts.append(f"- {r.text}")

    try:
        return call_gemini_json(GLOBAL_RULE_CHECK_SYSTEM, "\n".join(parts))
    except Exception:
        return None
