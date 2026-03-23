"""Global rule injection: pre-filters which global rules are relevant to a specific search. Uses Gemini."""

from __future__ import annotations

from search.gemini_helpers import call_gemini_json
from search.models import DefinedSearch, GlobalRule


FILTER_SYSTEM = """You are deciding which global rules to inject into a people search.

ONLY include a rule if the search would produce meaningfully different results with it. If a rule is about political vetting and the search has nothing to do with politics, do NOT include it. If a rule is about a specific domain and the search is about a different domain, do NOT include it.

When in doubt, EXCLUDE. Injecting irrelevant rules degrades search quality.

Respond as JSON:
{"relevant_ids": ["g004"], "reasoning": "brief explanation of why each was included or excluded"}

If NO rules are relevant, return {"relevant_ids": [], "reasoning": "none relevant"}."""


def filter_global_rules(
    search: DefinedSearch,
    all_rules: list[GlobalRule],
) -> list[GlobalRule]:
    """Determine which global rules are relevant to this search."""
    if not all_rules:
        return []

    parts = [f"SEARCH QUERY: {search.query}"]
    if search.clarification_context:
        parts.append(f"CLARIFICATION CONTEXT: {search.clarification_context}")
    if search.search_rules:
        parts.append("SEARCH-SPECIFIC RULES:")
        for r in search.search_rules:
            parts.append(f"- {r}")

    parts.append("\nGLOBAL RULES TO EVALUATE:")
    for rule in all_rules:
        parts.append(f"- ID: {rule.id} | Rule: {rule.text}")

    try:
        data = call_gemini_json(FILTER_SYSTEM, "\n".join(parts), max_tokens=512)
        relevant_ids = set(data.get("relevant_ids", []))
        search.applicable_global_rule_ids = list(relevant_ids)
        return [r for r in all_rules if r.id in relevant_ids]
    except Exception:
        return []  # If filter fails, inject nothing rather than everything
