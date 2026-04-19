"""Tests for the Gemini identity arbiter.

Covers:
- `arbitrate_identity` (enrichment/arbiter.py) — prompt construction, JSON
  parsing, error paths, abstention fallback. Gemini calls are stubbed; no
  real API requests are made.
- `IdentityResolver._score_candidates` arbiter integration — the arbiter
  IS called for tied / near-tied / at-threshold candidates, is NOT called
  for clear-cut winners, and its `winner_index` overrides the heuristic
  pick when set.
- `verification_decisions` records the arbiter call.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_arbiter.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment import arbiter as arbiter_mod
from enrichment.arbiter import (
    ARBITER_MODEL,
    _build_user_prompt,
    _parse_arbiter_response,
    arbitrate_identity,
)
from enrichment.identity import IdentityResolver
from enrichment.models import Profile


# ── Unit tests on arbiter internals ──────────────────────────────────


def test_build_user_prompt_includes_profile_and_candidates():
    profile = Profile(
        name="Will Poff-Webster",
        email="will@ifp.org",
        organization="Institute for Progress",
        title="Director",
    )
    candidates = [
        {
            "index": 0,
            "url": "https://linkedin.com/in/will-poff-webster",
            "title": "Will Poff-Webster — IFP",
            "description": "Director at Institute for Progress.",
            "score": 23,
            "reasons": ["first-in-title(+2)", "slug-name(+4)"],
        },
        {
            "index": 1,
            "url": "https://linkedin.com/in/will-smith-unrelated",
            "title": "Will Smith — Actor",
            "description": "Actor and producer.",
            "score": 23,
            "reasons": ["first-in-title(+2)"],
        },
    ]
    prompt = _build_user_prompt(profile, candidates)
    # Profile fields present
    assert "Will Poff-Webster" in prompt
    assert "will@ifp.org" in prompt
    assert "Institute for Progress" in prompt
    # Candidates present with indices
    assert "[0]" in prompt and "[1]" in prompt
    assert "linkedin.com/in/will-poff-webster" in prompt


def test_parse_arbiter_response_happy_path():
    text = json.dumps({"winner_index": 0, "confidence": "high", "reason": "exact org match"})
    out = _parse_arbiter_response(text, valid_indices={0, 1})
    assert out["winner_index"] == 0
    assert out["confidence"] == "high"
    assert out["reason"] == "exact org match"
    assert out["model"] == ARBITER_MODEL
    assert out["arbiter_called"] is True


def test_parse_arbiter_response_abstention():
    text = json.dumps({"winner_index": None, "confidence": "low", "reason": "cannot tell"})
    out = _parse_arbiter_response(text, valid_indices={0, 1})
    assert out["winner_index"] is None
    assert out["confidence"] == "low"


def test_parse_arbiter_response_hallucinated_index_is_discarded():
    text = json.dumps({"winner_index": 7, "confidence": "medium", "reason": "x"})
    out = _parse_arbiter_response(text, valid_indices={0, 1})
    # 7 is not in valid_indices → arbiter effectively abstains
    assert out["winner_index"] is None


def test_parse_arbiter_response_markdown_fenced():
    text = """```json
{"winner_index": 1, "confidence": "high", "reason": "slug matches"}
```"""
    out = _parse_arbiter_response(text, valid_indices={0, 1})
    assert out["winner_index"] == 1
    assert out["confidence"] == "high"


def test_parse_arbiter_response_garbage_returns_error_abstention():
    out = _parse_arbiter_response("not json at all", valid_indices={0})
    assert out["winner_index"] is None
    assert "arbiter_parse_error" in out["reason"]
    assert out.get("error") is True


def test_arbitrate_identity_handles_missing_sdk_without_raising():
    """If google.genai isn't installed / key missing, arbiter returns a
    structured error dict rather than raising."""
    profile = Profile(name="X")
    candidates = [
        {"index": 0, "url": "https://linkedin.com/in/a", "title": "a", "score": 5, "reasons": []},
        {"index": 1, "url": "https://linkedin.com/in/b", "title": "b", "score": 5, "reasons": []},
    ]
    with patch.object(arbiter_mod, "_get_client", side_effect=RuntimeError("GOOGLE_API_KEY not set")):
        out = arbitrate_identity(profile, candidates)
    assert out["winner_index"] is None
    assert out.get("error") is True
    assert out["arbiter_called"] is True


def test_arbitrate_identity_with_stubbed_client_returns_parsed_decision():
    profile = Profile(name="Dan Fragiadakis", email="dan@biotech.com")
    candidates = [
        {"index": 0, "url": "https://linkedin.com/in/dan-fragiadakis", "title": "Dan F", "score": 23, "reasons": []},
        {"index": 1, "url": "https://linkedin.com/in/dan-fake", "title": "Dan Fake", "score": 23, "reasons": []},
    ]
    fake_response = MagicMock()
    fake_response.text = json.dumps({"winner_index": 0, "confidence": "high", "reason": "slug + name"})
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    with patch.object(arbiter_mod, "_get_client", return_value=fake_client):
        out = arbitrate_identity(profile, candidates)
    assert out["winner_index"] == 0
    assert out["confidence"] == "high"
    assert out["arbiter_called"] is True


# ── Integration with _score_candidates ───────────────────────────────


def _resolver() -> IdentityResolver:
    return IdentityResolver(brave_api_key="", serper_api_key="")


def _ctx(**kwargs):
    base = {
        "name": kwargs.get("name", "Will Poff-Webster"),
        "first": "will",
        "last": "poff-webster",
        "email": "will@ifp.org",
        "email_domain": "ifp",
        "org": "Institute for Progress",
    }
    base.update(kwargs)
    return base


def test_arbiter_NOT_called_for_clear_cut_winner():
    """Single candidate with a dominant score — arbiter must NOT fire."""
    resolver = _resolver()
    candidates = [
        {"url": "https://linkedin.com/in/clearwinner", "title": "Will Poff-Webster IFP", "description": ""},
        {"url": "https://linkedin.com/in/long-way-behind", "title": "Other Person", "description": ""},
    ]
    profile = Profile(name="Will Poff-Webster", email="will@ifp.org", organization="Institute for Progress")
    log: list[str] = []

    with patch("enrichment.arbiter.arbitrate_identity") as mock_arb:
        # Make the first candidate a runaway winner by giving it strong signals
        # in title+description that are score +20 each.
        candidates[0]["title"] = "Will Poff-Webster — Institute for Progress (ifp)"
        candidates[0]["description"] = "Director at Institute for Progress — will@ifp.org"
        candidates[0]["_email_evidence_type"] = "exact"
        result = resolver._score_candidates(
            candidates, _ctx(), "Institute for Progress", log, profile=profile
        )
        assert mock_arb.call_count == 0, f"arbiter should not have been called; log:\n{log}"
    assert result.linkedin_url == "https://linkedin.com/in/clearwinner"


def test_arbiter_called_on_tie():
    """Two candidates tied at the top → arbiter consulted, winner overrides."""
    resolver = _resolver()
    candidates = [
        {
            "url": "https://linkedin.com/in/will-poff-webster-a",
            "title": "Will Poff-Webster Institute for Progress",
            "description": "Director at Institute for Progress ifp",
            "_email_evidence_type": "proximity",
        },
        {
            "url": "https://linkedin.com/in/will-poff-webster-b",
            "title": "Will Poff-Webster Institute for Progress",
            "description": "Director at Institute for Progress ifp",
            "_email_evidence_type": "proximity",
        },
    ]
    profile = Profile(name="Will Poff-Webster", email="will@ifp.org", organization="Institute for Progress")
    log: list[str] = []

    def fake_arb(prof, cands):
        # Arbiter picks index 1 (second candidate), overriding heuristic top.
        return {
            "winner_index": 1,
            "confidence": "high",
            "reason": "slug more distinctive",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
        }

    with patch("enrichment.arbiter.arbitrate_identity", side_effect=fake_arb) as mock_arb:
        result = resolver._score_candidates(
            candidates, _ctx(), "", log, profile=profile
        )
        assert mock_arb.call_count == 1, f"arbiter should have been called once; log:\n{log}"
    assert result.linkedin_url == "https://linkedin.com/in/will-poff-webster-b", (
        f"arbiter winner_index=1 should override heuristic top; got {result.linkedin_url}\nlog:\n{log}"
    )


def test_arbiter_abstention_skips_rather_than_accepts():
    """When the arbiter returns `winner_index: None`, we must return
    ambiguous (no URL) instead of silently accepting the heuristic top."""
    resolver = _resolver()
    candidates = [
        {
            "url": "https://linkedin.com/in/will-poff-webster-a",
            "title": "Will Poff-Webster Institute for Progress",
            "description": "Director at Institute for Progress ifp",
            "_email_evidence_type": "proximity",
        },
        {
            "url": "https://linkedin.com/in/will-poff-webster-b",
            "title": "Will Poff-Webster Institute for Progress",
            "description": "Director at Institute for Progress ifp",
            "_email_evidence_type": "proximity",
        },
    ]
    profile = Profile(name="Will Poff-Webster", email="will@ifp.org", organization="Institute for Progress")
    log: list[str] = []

    def fake_arb(prof, cands):
        return {
            "winner_index": None,
            "confidence": "low",
            "reason": "both plausible, cannot distinguish",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
        }

    with patch("enrichment.arbiter.arbitrate_identity", side_effect=fake_arb) as mock_arb:
        result = resolver._score_candidates(
            candidates, _ctx(), "", log, profile=profile
        )
        assert mock_arb.call_count == 1
    assert result.linkedin_url == "", f"Expected abstention → no URL; got {result.linkedin_url}"
    # Still records the arbiter call on the profile
    assert profile.verification_decisions
    last = profile.verification_decisions[-1]
    assert last["decision"] == "ambiguous"
    assert "arbiter abstained" in last["reason"]


def test_arbiter_called_at_most_once_per_profile():
    """Arbiter fires at most once regardless of how many groups tie."""
    resolver = _resolver()
    # Three-way tie at the top; one call. Using proximity-email evidence to
    # push candidates above threshold so they actually reach the tie-break.
    candidates = [
        {
            "url": f"https://linkedin.com/in/will-poff-webster-{i}",
            "title": "Will Poff-Webster Institute for Progress",
            "description": "Director at Institute for Progress ifp",
            "_email_evidence_type": "proximity",
        }
        for i in range(3)
    ]
    profile = Profile(name="Will Poff-Webster", email="will@ifp.org", organization="Institute for Progress")
    log: list[str] = []

    def fake_arb(prof, cands):
        return {
            "winner_index": 0,
            "confidence": "medium",
            "reason": "first is best",
            "model": ARBITER_MODEL,
            "arbiter_called": True,
        }

    with patch("enrichment.arbiter.arbitrate_identity", side_effect=fake_arb) as mock_arb:
        resolver._score_candidates(
            candidates, _ctx(), "", log, profile=profile
        )
        assert mock_arb.call_count == 1, (
            f"Arbiter must be called at most once per profile; got {mock_arb.call_count}"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
