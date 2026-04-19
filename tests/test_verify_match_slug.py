"""Regression tests for the P4 slug-aware verification bonus.

Covers `enrichment/enrichers.py::_verify_match` (signature extended to take
`linkedin_url`) as described in plans/diagnosis_correctness.md P4.

Contract under test:
- Slug contains profile's first name AND last name → +2 positive non-name
  signal (counts toward corroboration, can unlock borderline acceptance).
- Slug contains only first name → 0 (neutral — no bonus, no penalty).
  This is intentional: legitimate "LinkedIn hides last name" cases should
  not be punished for the absence of a slug corroboration anchor.
- Slug contains neither → 0 (neutral). Numeric / auto-generated slugs
  should not be penalised.
- Slug contains the ENRICHED person's last name but NOT the profile's
  last name → counter-signal logged, no score subtraction (the name-match
  guards upstream handle the reject; this is purely for observability).
- Missing `linkedin_url` argument → slug check is skipped, scoring falls
  back to the pre-P4 behaviour.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_verify_match_slug.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.enrichers import LinkedInEnricher
from enrichment.models import Profile


def _enricher() -> LinkedInEnricher:
    return LinkedInEnricher(api_key="")


def _enriched(**kwargs) -> dict:
    base = {
        "full_name": "",
        "headline": "",
        "current_company": "",
        "current_title": "",
        "location": "",
        "summary": "",
        "experience": [],
        "education": [],
        "context_block": "",
    }
    base.update(kwargs)
    return base


# ── Slug bonus fires on full-name match ─────────────────────────

def test_slug_full_name_match_adds_positive_signal():
    """Slug with both first AND last name corroborates the match.
    Slug bonus adds to the score (+2) but does NOT count as an
    independent positive non-name signal. Rationale: same-name collisions
    always produce matching slugs, so the slug doesn't corroborate
    anything the name hasn't already said. An org mismatch on a non-vague
    org therefore still rejects, which prevents the Abigail Olvera case
    (matched to Mexican lawyer via common-name slug)."""
    enricher = _enricher()
    profile = Profile(
        name="Dan Fragiadakis",
        organization="Different Org Than LinkedIn Shows",
    )
    enriched = _enriched(
        full_name="Dan Fragiadakis",
        current_company="Biotech Ventures",
        experience=[{"company": "Biotech Ventures", "title": "Founder"}],
    )
    url = "https://www.linkedin.com/in/dan-fragiadakis-phd"
    verified, log = enricher._verify_match(profile, enriched, url)
    joined = "\n".join(log)
    # Slug match must have been logged.
    assert "Verify slug: MATCH" in joined, f"Expected slug MATCH; log:\n{joined}"
    # Rejected: org mismatch with no INDEPENDENT positive signal.
    assert not verified, f"Expected REJECT (slug alone can't carry through org mismatch); log:\n{joined}"
    # Structured observability: slug_match still recorded as an anchor.
    decisions = profile.verification_decisions
    assert decisions
    last = decisions[-1]
    assert "slug_match" in last["anchors_positive"], f"anchors_positive={last['anchors_positive']}"
    assert "org_mismatch" in last["anchors_negative"]
    assert last["decision"] == "reject"


def test_slug_only_first_name_no_penalty():
    """Slug with only first name is NEUTRAL — LinkedIn commonly hides last
    names for privacy, and a first-only slug is not counter-evidence."""
    enricher = _enricher()
    profile = Profile(
        name="Allan Buchness",
        organization="",
    )
    enriched = _enriched(
        full_name="Allan Buchness",
        current_company="Some Co",
        experience=[{"company": "Some Co", "title": "Engineer"}],
    )
    url = "https://www.linkedin.com/in/allan-jabri-xyz"  # has "allan" but NOT "buchness"
    # With the strong name match carrying the decision, the verifier should
    # still accept. But critically the slug must not have fired MATCH nor
    # added a slug_match anchor.
    verified, log = enricher._verify_match(profile, enriched, url)
    joined = "\n".join(log)
    assert "Verify slug: PARTIAL" in joined, f"Expected PARTIAL slug signal; log:\n{joined}"
    assert "Verify slug: MATCH" not in joined
    decisions = profile.verification_decisions
    assert decisions
    assert "slug_match" not in decisions[-1]["anchors_positive"]
    # Note: accept/reject outcome depends on other fields; here the name match
    # is strong with no soft penalty, so we expect accept.
    assert verified


def test_slug_neither_name_neutral():
    """Slug with neither name (e.g., numeric / auto-generated) is neutral."""
    enricher = _enricher()
    profile = Profile(
        name="Renee DiResta",
        organization="Stanford",
    )
    enriched = _enriched(
        full_name="Renee DiResta",
        current_company="Stanford Internet Observatory",
        experience=[{"company": "Stanford Internet Observatory", "title": "Research Manager"}],
    )
    # Slug has no name tokens (numeric auto-generated)
    url = "https://www.linkedin.com/in/abc-123xyz-987"
    verified, log = enricher._verify_match(profile, enriched, url)
    joined = "\n".join(log)
    # Should NOT have slug MATCH anchor; logs "NONE" or a counter-signal.
    assert "Verify slug: MATCH" not in joined
    # Name + org still enough for accept
    assert verified, f"Expected ACCEPT on name+org even without slug; log:\n{joined}"
    decisions = profile.verification_decisions
    assert decisions
    assert "slug_match" not in decisions[-1]["anchors_positive"]


def test_slug_matches_enriched_last_is_counter_signal():
    """When slug corroborates the ENRICHED person's last name but NOT the
    profile's, we log a counter-signal but do NOT subtract (the name-match
    guard upstream already hard-rejects last-name-missing). This case should
    get caught earlier by the existing WEAK-MATCH REJECTED path; we're
    asserting the anchor is recorded when we're able to reach the slug
    check (e.g., when the last-name does appear in the enriched name, just
    not as the primary last name)."""
    enricher = _enricher()
    profile = Profile(
        name="Abi Olvera",
        organization="",
    )
    # Enriched has "Abi Hashem" — short-match guard should fire first.
    enriched = _enriched(
        full_name="Abi Hashem",
        current_company="Counseling Center",
        experience=[{"company": "Counseling Center", "title": "Therapist"}],
    )
    url = "https://www.linkedin.com/in/abi-hashem-phd"
    verified, log = enricher._verify_match(profile, enriched, url)
    joined = "\n".join(log)
    # This should reject via the upstream WEAK-MATCH guard.
    assert not verified, f"Expected REJECT for wrong person; log:\n{joined}"
    # The verification_decisions must capture the reject.
    decisions = profile.verification_decisions
    assert decisions
    assert decisions[-1]["decision"] == "reject"


def test_slug_mismatch_does_not_penalize_valid_abbreviated_name():
    """Regression: a legitimate profile whose LinkedIn slug differs (e.g.,
    slug is numeric or abbreviated) must not be wrongly rejected just
    because the slug check returns NONE. Slug is only additive — never a
    subtractor."""
    enricher = _enricher()
    profile = Profile(
        name="Dylan Matthews",
        organization="Vox",
    )
    enriched = _enriched(
        full_name="Dylan Matthews",
        current_company="Vox",
        experience=[{"company": "Vox", "title": "Senior Correspondent"}],
    )
    # Slug is numeric — no name tokens.
    url = "https://www.linkedin.com/in/1a2b3c4d"
    verified, log = enricher._verify_match(profile, enriched, url)
    joined = "\n".join(log)
    assert verified, f"Expected ACCEPT for name+org match even with unrecognisable slug; log:\n{joined}"


def test_missing_linkedin_url_falls_back_to_prefix_behavior():
    """Backwards-compat: callers that don't pass linkedin_url must get the
    pre-P4 scoring."""
    enricher = _enricher()
    profile = Profile(
        name="Renee DiResta",
        organization="Stanford",
    )
    enriched = _enriched(
        full_name="Renee DiResta",
        current_company="Stanford Internet Observatory",
        experience=[{"company": "Stanford Internet Observatory", "title": "Research Manager"}],
    )
    # Call without linkedin_url argument
    verified, log = enricher._verify_match(profile, enriched)
    assert verified
    joined = "\n".join(log)
    assert "Verify slug" not in joined, "slug check should not fire without URL"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
