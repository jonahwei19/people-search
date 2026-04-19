"""Gemini-based identity arbiter for ambiguous identity-resolution ties.

When `_score_candidates` produces 2+ candidates within 1 score-point of each
other, or when the top candidate accepts exactly at threshold (a coin-flip
regime), we ask Gemini Flash-Lite which candidate, if any, actually matches
the uploaded profile. The arbiter is ONLY consulted on genuinely ambiguous
cases — clear-cut winners bypass it to keep cost down (target <$0.001/call).

Contract
--------
`arbitrate_identity(profile, candidates) -> dict`

Input
-----
- `profile`: an `enrichment.models.Profile` with `name`, `email`, `organization`,
  `title`, and any content fields populated. Used to build the prompt.
- `candidates`: a list of dicts, each describing one LinkedIn candidate:
    {
      "index": int,              # position in the original candidates list (caller's choice)
      "url": str,                # https://linkedin.com/in/...
      "title": str,              # search-result title (often the person's name + headline)
      "description": str,        # search-result snippet (optional)
      "score": int | None,       # heuristic score (for the prompt's context)
      "reasons": list[str],      # scoring reason tags (e.g., ["email-evidence(+20)", ...])
    }

Output (JSON dict)
------------------
    {
      "winner_index": int | None,        # None = arbiter abstains (all wrong or can't tell)
      "confidence": "high" | "medium" | "low",
      "reason": str,                     # one-line justification
      "model": str,                      # model id used
      "arbiter_called": True,
    }

On any error or API failure, returns
    {"winner_index": None, "confidence": "low", "reason": "arbiter_error: ...",
     "model": ..., "arbiter_called": True, "error": True}

See plans/diagnosis_hitrate.md §91-ambiguous-tie analysis and
plans/diagnosis_correctness.md §tie-break section.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

try:
    from google import genai
except ImportError:  # pragma: no cover — handled at call site
    genai = None  # type: ignore

from ._retry import retry_request

# Cheapest Gemini model already in use elsewhere in this codebase
# (search/gemini_helpers.py and search/llm_judge.py). Keeping it consistent
# so the org's Gemini cost accounting stays predictable.
ARBITER_MODEL = "gemini-3.1-flash-lite-preview"

# Hard cap on the number of candidates we feed the arbiter. More than this
# dilutes the prompt and eats tokens — if we can't pick between 5 we should
# just reject as ambiguous.
MAX_CANDIDATES = 5


_SYSTEM_PROMPT = """You are an identity-verification arbiter.

You are shown a PROFILE (the person we're trying to match) and a list of
LINKEDIN CANDIDATES. Decide which candidate, if any, is actually the same
person as the profile. Some candidates may be same-name-different-person
false positives — be conservative.

Respond with STRICT JSON of the form:
  {"winner_index": <int-or-null>, "confidence": "high"|"medium"|"low", "reason": "<one-line>"}

Rules:
- winner_index MUST be one of the candidate indices shown, or null if no
  candidate clearly matches the profile.
- Prefer null over guessing. A wrong pick is worse than abstaining.
- Use email-domain alignment, org/employer overlap, title/role overlap,
  slug-name match, location, and content relevance as evidence.
- "high" confidence requires multiple corroborating signals (e.g., name +
  org + slug, or name + email-domain + title).
- "low" = ambiguous; if you're at "low" and it's a coin flip, set
  winner_index to null.
"""


def _build_user_prompt(profile: Any, candidates: list[dict]) -> str:
    """Render the profile + candidates as a compact Gemini user prompt."""
    parts: list[str] = []
    parts.append("PROFILE:")
    parts.append(f"  name: {getattr(profile, 'name', '') or ''}")
    parts.append(f"  email: {getattr(profile, 'email', '') or ''}")
    parts.append(f"  organization: {getattr(profile, 'organization', '') or ''}")
    parts.append(f"  title: {getattr(profile, 'title', '') or ''}")

    # Short bio: first ~500 chars of any content field.
    content_fields = getattr(profile, "content_fields", None) or {}
    bio_snippets = []
    for key, val in content_fields.items():
        if not val:
            continue
        bio_snippets.append(f"  {key}: {str(val)[:500]}")
        if sum(len(s) for s in bio_snippets) > 1500:
            break
    if bio_snippets:
        parts.append("  content:")
        parts.extend(bio_snippets)

    parts.append("")
    parts.append("CANDIDATES:")
    for c in candidates:
        parts.append(f"  [{c.get('index')}] {c.get('url', '')}")
        if c.get("title"):
            parts.append(f"    title: {c['title']}")
        if c.get("description"):
            desc = str(c["description"])[:500]
            parts.append(f"    snippet: {desc}")
        if c.get("score") is not None:
            parts.append(f"    heuristic_score: {c['score']}")
        reasons = c.get("reasons") or []
        if reasons:
            parts.append(f"    reasons: {', '.join(reasons)}")

    parts.append("")
    parts.append(
        "Respond with JSON: {\"winner_index\": <int-or-null>, "
        "\"confidence\": \"high\"|\"medium\"|\"low\", \"reason\": <string>}"
    )
    return "\n".join(parts)


def _parse_arbiter_response(text: str, valid_indices: set[int]) -> dict[str, Any]:
    """Parse Gemini output into the arbiter contract shape."""
    # Strip markdown code fences if present
    t = text.strip()
    if t.startswith("```"):
        # find the first { and last }
        s = t.find("{")
        e = t.rfind("}")
        if s >= 0 and e > s:
            t = t[s : e + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        # Best-effort brace extraction
        s = t.find("{")
        e = t.rfind("}")
        if s >= 0 and e > s:
            try:
                data = json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                return {
                    "winner_index": None,
                    "confidence": "low",
                    "reason": f"arbiter_parse_error: {t[:100]}",
                    "model": ARBITER_MODEL,
                    "arbiter_called": True,
                    "error": True,
                }
        else:
            return {
                "winner_index": None,
                "confidence": "low",
                "reason": f"arbiter_parse_error: {t[:100]}",
                "model": ARBITER_MODEL,
                "arbiter_called": True,
                "error": True,
            }

    winner = data.get("winner_index")
    if winner is not None:
        try:
            winner = int(winner)
        except (TypeError, ValueError):
            winner = None
        if winner is not None and winner not in valid_indices:
            # Arbiter hallucinated an index — abstain rather than pick wrong.
            winner = None

    confidence = str(data.get("confidence") or "low").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    reason = str(data.get("reason") or "").strip() or "no-reason"

    return {
        "winner_index": winner,
        "confidence": confidence,
        "reason": reason,
        "model": ARBITER_MODEL,
        "arbiter_called": True,
    }


def _get_client():
    """Lazy Gemini client. Raises RuntimeError if the SDK is missing / no key."""
    if genai is None:
        raise RuntimeError("google.genai not installed")
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set")
    return genai.Client(api_key=api_key)


def arbitrate_identity(profile: Any, candidates: list[dict]) -> dict[str, Any]:
    """Ask Gemini to pick the correct candidate from an ambiguous tie.

    See module docstring for the full contract. Errors return a structured
    abstention dict rather than raising — callers are expected to treat an
    abstention as "skip, don't accept".
    """
    if not candidates or len(candidates) < 2:
        # Arbiter only makes sense on 2+ candidates. Caller should have
        # guarded this, but defend here anyway.
        return {
            "winner_index": candidates[0]["index"] if candidates else None,
            "confidence": "low",
            "reason": "arbiter_skipped: <2 candidates",
            "model": ARBITER_MODEL,
            "arbiter_called": False,
        }

    # Cap candidate list at MAX_CANDIDATES; the scoring caller has already
    # sorted by heuristic score descending.
    candidates = candidates[:MAX_CANDIDATES]
    valid_indices = {c["index"] for c in candidates if "index" in c}

    try:
        client = _get_client()
    except RuntimeError as e:
        return {
            "winner_index": None,
            "confidence": "low",
            "reason": f"arbiter_error: {e}",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
            "error": True,
        }

    user_prompt = _build_user_prompt(profile, candidates)

    def _call():
        return client.models.generate_content(
            model=ARBITER_MODEL,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                max_output_tokens=256,
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )

    # Wrap in the shared retry helper so transient Gemini errors don't
    # silently sink the arbitration.
    response = retry_request(
        _call,
        max_attempts=2,
        base_delay=1.0,
        label=f"Gemini arbiter {getattr(profile, 'name', '')[:32]}",
        on_5xx=False,  # retry_request's 5xx path expects a requests.Response
    )

    if response is None:
        return {
            "winner_index": None,
            "confidence": "low",
            "reason": "arbiter_error: exhausted retries",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
            "error": True,
        }

    try:
        text = response.text or ""
    except Exception as e:  # pragma: no cover
        print(f"  [arbiter] response.text failed: {e}", file=sys.stderr)
        return {
            "winner_index": None,
            "confidence": "low",
            "reason": f"arbiter_error: {e}",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
            "error": True,
        }

    return _parse_arbiter_response(text, valid_indices)
