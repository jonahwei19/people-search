"""LLM-as-Judge using Gemini 2.5 Flash-Lite. ~$0.03 per search over 400 profiles."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from google import genai

from search import config
from search.models import DefinedSearch, GlobalRule, Profile, ScoreResult

PROFILE_CHAR_LIMIT = 3000
BATCH_SIZE = 40
MAX_CONCURRENT = 5
MODEL = "gemini-3.1-flash-lite-preview"


def get_client():
    api_key = os.environ.get("GOOGLE_API_KEY", config.GOOGLE_API_KEY if hasattr(config, "GOOGLE_API_KEY") else "")
    return genai.Client(api_key=api_key)


def build_system_prompt(
    search: DefinedSearch,
    applicable_global_rules: list[GlobalRule],
) -> str:
    parts = ["You are scoring candidates for a people search.\n"]
    parts.append(f"QUERY: {search.query}")

    if search.clarification_context:
        parts.append(f"\nCONTEXT:\n{search.clarification_context}")

    if applicable_global_rules:
        parts.append("\nGLOBAL RULES:")
        for rule in applicable_global_rules:
            parts.append(f"- {rule.text}")

    if search.search_rules:
        parts.append("\nSEARCH RULES:")
        for rule in search.search_rules:
            parts.append(f"- {rule}")

    if search.exemplars:
        parts.append("\nCALIBRATION EXAMPLES:")
        for ex in sorted(search.exemplars, key=lambda e: -e.score):
            parts.append(f"Score {ex.score}: {ex.profile_name} — {ex.profile_summary[:200]} — \"{ex.reason}\"")

    # Prompt corrections from user-edited reasoning
    if search.prompt_corrections:
        parts.append("\nCORRECTIONS (from user feedback — follow these strictly):")
        for correction in search.prompt_corrections:
            parts.append(f"- {correction}")

    parts.append("""
Score each candidate 0-100. Return a JSON array, one entry per candidate:
[{"id": "profile_id", "s": N, "r": "brief reason"}]

SCORING CALIBRATION — follow these constraints strictly:
- 95-100: Reserve for 1-2 candidates per batch AT MOST. Must be a near-perfect match.
- 85-94: Strong match with clear, specific evidence. No more than 10-15% of candidates.
- 70-84: Good match but missing something. This is where most decent candidates land.
- 50-69: Partial match. Relevant background but significant gaps.
- 30-49: Weak. Tangentially related at best.
- 0-29: Not a match.

Most candidates should score 20-60. If you're giving more than 3 candidates a 90+, you are being too generous — go back and differentiate. Use the FULL range.

HARD FILTERS: If the query specifies a hard criterion (e.g., "worked at TikTok", "lives in London", "has a PhD"), anyone who does NOT meet that criterion MUST score 0. Do NOT give partial credit for adjacent experience. "Worked at Google" is NOT a match for "worked at TikTok." Apply the filter strictly BEFORE scoring quality.

DIFFERENTIATION: Two candidates should NOT get the same score unless they are genuinely indistinguishable. If you're about to give the same score to multiple people, rank them relative to each other and spread by at least 3 points.

Keep reasons to <15 words. Be specific — name the key signal, not a generic description.""")

    return "\n".join(parts)


def build_batch_user_prompt(profiles: list[Profile]) -> str:
    parts = []
    for p in profiles:
        text = p.raw_text[:PROFILE_CHAR_LIMIT]
        parts.append(f"ID:{p.id}\n{p.identity.name or 'Unknown'}\n{text}\n---")
    return "\n".join(parts)


def parse_response(text: str, profiles: list[Profile]) -> dict[str, ScoreResult]:
    """Parse JSON response into ScoreResults."""
    # Strip markdown code fences
    if "```" in text:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            text = text[start:end]

    results = {}
    try:
        data = json.loads(text)
        if isinstance(data, list):
            for item in data:
                pid = item.get("id", "")
                score = max(0, min(100, int(item.get("s", item.get("score", 0)))))
                reasoning = item.get("r", item.get("reasoning", ""))
                results[pid] = ScoreResult(score=score, reasoning=reasoning)
        elif isinstance(data, dict):
            for pid, val in data.items():
                if isinstance(val, (int, float)):
                    results[pid] = ScoreResult(score=max(0, min(100, int(val))), reasoning="")
                elif isinstance(val, dict):
                    results[pid] = ScoreResult(
                        score=max(0, min(100, int(val.get("s", val.get("score", 0))))),
                        reasoning=val.get("r", val.get("reasoning", "")),
                    )
    except (json.JSONDecodeError, TypeError, ValueError):
        # Try extracting JSON from text
        for ch in ["[", "{"]:
            start = text.find(ch)
            end_ch = "]" if ch == "[" else "}"
            end = text.rfind(end_ch) + 1
            if start >= 0 and end > start:
                try:
                    return parse_response(text[start:end], profiles)
                except Exception:
                    pass
        print(f"  Parse error: {text[:300]}...")

    # Fill missing
    for p in profiles:
        if p.id not in results:
            results[p.id] = ScoreResult(score=0, reasoning="")

    return results


def score_batch_sync(
    client: genai.Client,
    system_prompt: str,
    profiles: list[Profile],
) -> dict[str, ScoreResult]:
    """Score a batch of profiles synchronously."""
    user_prompt = build_batch_user_prompt(profiles)

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=8192,
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        return parse_response(response.text, profiles)
    except Exception as e:
        print(f"  API error: {e}")
        return {p.id: ScoreResult(score=0, reasoning=f"Error: {e}") for p in profiles}


def score_profiles_sync(
    search: DefinedSearch,
    profiles: list[Profile],
    applicable_global_rules: list[GlobalRule],
    progress_callback: Optional[callable] = None,
) -> dict[str, ScoreResult]:
    """Score all profiles against a search."""
    client = get_client()
    system_prompt = build_system_prompt(search, applicable_global_rules)

    batches = [profiles[i:i + BATCH_SIZE] for i in range(0, len(profiles), BATCH_SIZE)]
    print(f"Scoring {len(profiles)} profiles in {len(batches)} batches (Gemini Flash-Lite)...")

    all_results: dict[str, ScoreResult] = {}
    completed = 0

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = {
            executor.submit(score_batch_sync, client, system_prompt, batch): batch
            for batch in batches
        }
        for future in concurrent.futures.as_completed(futures):
            batch = futures[future]
            result = future.result()
            all_results.update(result)
            completed += len(batch)
            if progress_callback:
                progress_callback(completed, len(profiles))
            else:
                print(f"  {completed}/{len(profiles)}")

    return all_results


def rank_results(
    scores: dict[str, ScoreResult],
    profiles: list[Profile],
) -> list[tuple[Profile, ScoreResult]]:
    """Return profiles sorted by score descending."""
    scored = [(p, scores[p.id]) for p in profiles if p.id in scores]
    scored.sort(key=lambda x: (-x[1].score, x[0].identity.name or ""))
    return scored


if __name__ == "__main__":
    from data_loader import load_profiles
    from models import GlobalRules

    profiles = load_profiles(config.DATA_DIR)
    global_rules = GlobalRules.load(config.GLOBAL_RULES_PATH)

    search = DefinedSearch(
        name="test",
        query="someone with experience scaling organizations or running operations",
    )

    t0 = time.time()
    scores = score_profiles_sync(search, profiles, global_rules.rules)
    elapsed = time.time() - t0

    ranked = rank_results(scores, profiles)
    print(f"\nDone in {elapsed:.1f}s. Top 15:")
    for i, (p, s) in enumerate(ranked[:15]):
        print(f"  {i+1}. [{s.score}] {p.identity.name}: {s.reasoning}")

    # Check Byron Edwards specifically
    for i, (p, s) in enumerate(ranked):
        if p.identity.name and "byron" in p.identity.name.lower():
            print(f"\nByron Edwards: rank {i+1}, score {s.score}, reason: {s.reasoning}")
            break
