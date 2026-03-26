"""Feedback system: collects ratings, synthesizes rules, manages exemplars. Uses Gemini."""

from __future__ import annotations

from typing import Optional

from search.gemini_helpers import call_gemini_json
from search.models import (
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

You have the FULL PROFILE DATA for each feedback event. Use it to understand WHY the user accepted or rejected each person — don't just echo their reason, look at the actual profile.

Do four things:

1. PATTERN DETECTION: Look across ALL feedback events for patterns. If 3 people were rejected and they're all academics with no shipping experience, that's one rule — not three separate observations. Be specific: "Exclude profiles whose only experience is academic research positions" is better than "Exclude academics."

2. RULES: Propose 0-3 new search-specific rules based on patterns. Each rule must be:
   - Actionable (a scorer can apply it)
   - Specific (names the signal, not a vague category)
   - Pattern-based (derived from 2+ feedback events, not just one)

3. EXEMPLAR CHANGES: Strong accepts → positive exemplars (score 85-95). Strong rejects → negative exemplars (score 5-15). Include the KEY DISTINGUISHING FEATURE in the reason — what makes this person good/bad for this search.

4. CONSOLIDATION: If existing rules overlap with new ones, merge them. Remove redundant exemplars. Keep total rules under 10 and exemplars under 10.

Respond as JSON:
{
  "new_rules": ["specific, actionable rule text", ...],
  "modified_rules": [{"old": "old text", "new": "new text"}, ...],
  "remove_rules": ["rule text to remove", ...],
  "add_exemplars": [{"profile_id": "id", "profile_name": "name", "score": N, "reason": "key distinguishing feature"}],
  "remove_exemplar_ids": ["id", ...],
  "notes": "brief explanation of patterns found"
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


CLASSIFY_FEEDBACK_SYSTEM = """Classify this feedback event into exactly one category.

Categories:
- "profile": The feedback is about THIS SPECIFIC PERSON being wrong/right for the search (e.g., "too academic", "great operator", "wrong field")
- "scoring": The feedback is about the JUDGE'S REASONING being wrong (e.g., user corrected "strong builder" to "no evidence of shipping" — the judge misread the profile)
- "global": The feedback applies to ALL searches, not just this one (e.g., "founders of high-growth startups are locked-in" is always true)

Also extract the key signal — the specific, concrete thing that makes this profile good or bad.

Respond as JSON:
{"category": "profile|scoring|global", "key_signal": "one specific concrete signal", "prompt_correction": "if scoring category: CORRECTION: When you see X, do Y instead of Z"}"""


def classify_feedback(
    search: DefinedSearch,
    event: FeedbackEvent,
    profile: Optional[Profile] = None,
) -> dict:
    """Classify a feedback event and extract the key signal."""
    parts = [
        f"SEARCH: {search.name}",
        f"QUERY: {search.query}",
        f"RATING: {event.rating} on {event.profile_name}",
    ]
    if event.reason:
        parts.append(f"REASON: {event.reason}")
    if event.reasoning_correction:
        parts.append(f"USER CORRECTED REASONING FROM: (judge said something) TO: {event.reasoning_correction}")
    if profile:
        parts.append(f"PROFILE:\n{profile.raw_text[:400]}")
    # Include judge's score/reasoning if available
    if search.cache.scores and event.profile_id in search.cache.scores:
        sr = search.cache.scores[event.profile_id]
        parts.append(f"JUDGE SCORED: {sr.score}, JUDGE SAID: {sr.reasoning}")

    try:
        return call_gemini_json(CLASSIFY_FEEDBACK_SYSTEM, "\n".join(parts))
    except Exception:
        return {"category": "profile", "key_signal": event.reason or "unknown", "prompt_correction": ""}


EXTRACT_POSITIVE_SYSTEM = """The user marked this candidate as excellent for their search.
Look at the profile and identify the KEY DISTINGUISHING FEATURES that make this person stand out.
Be specific — name concrete signals (titles, companies, projects, skills) not vague qualities.

Respond as JSON:
{"key_features": ["feature 1", "feature 2"], "summary": "one sentence: why this person is great for this search"}"""


def extract_positive_signal(
    search: DefinedSearch,
    profile: Profile,
) -> dict:
    """Extract what makes a positively-rated profile good for this search."""
    parts = [
        f"SEARCH: {search.name}",
        f"QUERY: {search.query}",
        f"EXCELLENT CANDIDATE: {profile.identity.name or 'Unknown'}",
        f"PROFILE:\n{profile.raw_text[:500]}",
    ]
    if search.search_rules:
        parts.append("SEARCH RULES:")
        for r in search.search_rules:
            parts.append(f"- {r}")

    try:
        return call_gemini_json(EXTRACT_POSITIVE_SYSTEM, "\n".join(parts))
    except Exception:
        return {"key_features": [], "summary": "Marked as excellent by user"}


INFER_REASON_SYSTEM = """The user rejected this candidate for a search but didn't say why.
Based on the search query and the candidate's profile, infer the most likely reason in ONE sentence (max 15 words).
Focus on the GAP — what's missing or wrong, not what's there.

Respond as JSON:
{"reason": "one sentence reason", "key_signal": "the specific thing that makes them a bad match"}"""


def infer_rejection_reason(
    search: DefinedSearch,
    profile: Profile,
) -> dict:
    """Auto-generate a reason when user rejects without explaining."""
    parts = [
        f"SEARCH: {search.name}",
        f"QUERY: {search.query}",
    ]
    if search.search_rules:
        parts.append("SEARCH RULES:")
        for r in search.search_rules:
            parts.append(f"- {r}")
    parts.append(f"\nREJECTED CANDIDATE: {profile.identity.name or 'Unknown'}")
    parts.append(f"PROFILE:\n{profile.raw_text[:500]}")

    try:
        return call_gemini_json(INFER_REASON_SYSTEM, "\n".join(parts))
    except Exception as e:
        return {"reason": "Rejected without reason", "key_signal": "unknown"}


def create_negative_exemplar(
    search: DefinedSearch,
    profile: Profile,
    reason: str,
) -> None:
    """Add a profile as a negative exemplar (score 5) to calibrate the scorer."""
    # Remove existing exemplar for this profile if any
    search.exemplars = [ex for ex in search.exemplars if ex.profile_id != profile.id]

    # Cap total exemplars
    if len(search.exemplars) >= MAX_EXEMPLARS:
        # Remove the oldest low-score exemplar to make room
        low_exemplars = [ex for ex in search.exemplars if ex.score <= 20]
        if low_exemplars:
            search.exemplars.remove(low_exemplars[0])

    search.exemplars.append(Exemplar(
        profile_id=profile.id,
        profile_name=profile.identity.name or "Unknown",
        profile_summary=profile.raw_text[:300],
        score=5,
        reason=reason,
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
