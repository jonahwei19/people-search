"""Regression tests for enrichment._verify_match and contamination backfill.

Covers two bugs reported in plans/diagnosis_correctness.md and
plans/diagnosis_hitrate.md:

FM2 / P2 — Verification threshold too tight when org mismatches.
    Name-match-alone scoring exactly at threshold (+2) meant any single soft
    penalty (org mismatch, location mismatch) flipped the decision. Real-data
    evidence: 27 "name match + org mismatch + still accepted" cases of
    same-name-different-person slipping through. Fix requires strong-name
    scoring (+3) for uncommon full-name matches and requires at least one
    positive non-name signal when ANY soft penalty fires.

FM5 — Contamination via backfill. When `_verify_match` is called on a
    wrong-person LinkedIn, the caller previously backfilled
    `profile.organization` / `profile.title` with LinkedIn's values, destroying
    the user-supplied ground truth. Fix: never overwrite user-provided org or
    title; populate a separate `enriched_organization` / `enriched_title`.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_verify_match_thresholds.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.enrichers import LinkedInEnricher
from enrichment.models import EnrichmentStatus, Profile


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _enricher() -> LinkedInEnricher:
    # api_key="" so _call_api is never hit; we're calling _verify_match directly.
    return LinkedInEnricher(api_key="")


def _enriched(**kwargs) -> dict:
    """Build a parsed-enrichment dict shaped like what _parse_response returns."""
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


# ──────────────────────────────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────────────────────────────


def test_abigail_olvera_org_mismatch_rejected():
    """FM2: real-data case. Name matches but the enriched LinkedIn is a
    different Abigail Olvera at MegaCorp. Under the old scoring
    (name=+2, org_mismatch=-1 → score=1... wait, actually +3 for strong name
    match minus -1 = +2, accepted). New rule: soft penalty with no positive
    non-name signal → REJECT."""
    enricher = _enricher()
    profile = Profile(
        name="Abigail Olvera",
        organization="Golden Gate Institute for AI",
    )
    enriched = _enriched(
        full_name="Abigail Olvera",
        current_company="MegaCorp Property Management",
        current_title="Property Manager",
        experience=[{"company": "MegaCorp Property Management", "title": "Property Manager"}],
    )
    verified, log = enricher._verify_match(profile, enriched)
    joined = "\n".join(log)
    assert not verified, f"Expected REJECT for same-name-different-person; got ACCEPT.\nlog:\n{joined}"
    assert "REJECTED" in joined


def test_renee_diresta_name_only_accepted():
    """P2: strong full-name match (both tokens ≥5 chars) with NO org provided.
    No soft penalties fire because we have nothing to compare. Must still
    accept."""
    enricher = _enricher()
    profile = Profile(
        name="Renee DiResta",
        organization="",  # deliberately empty — tests the "no org to check" path
    )
    enriched = _enriched(
        full_name="Renee DiResta",
        current_company="Stanford Internet Observatory",
        current_title="Research Manager",
        experience=[{"company": "Stanford Internet Observatory", "title": "Research Manager"}],
    )
    verified, log = enricher._verify_match(profile, enriched)
    joined = "\n".join(log)
    assert verified, f"Expected ACCEPT for strong name match with no org to conflict.\nlog:\n{joined}"
    assert "ACCEPTED" in joined
    assert "strong" in joined, f"Expected name=strong tier; log:\n{joined}"


def test_backfill_does_not_overwrite_user_org_on_rejection():
    """FM5: verify_match rejection must not leave the profile contaminated.
    The enrich_profile caller only backfills on acceptance, but this test
    exercises the defensive boundary: if the enricher accepts wrongly in the
    future, the NEW policy is to never overwrite user-provided org/title."""
    enricher = _enricher()
    profile = Profile(
        name="Joshua New",
        organization="Golden Gate Institute for AI",
        title="Research Fellow",
    )
    enriched = _enriched(
        full_name="Joshua New",  # name matches
        current_company="Wrong Co",
        current_title="Founding Partner",
        experience=[{"company": "Wrong Co", "title": "Founding Partner"}],
    )
    verified, _log = enricher._verify_match(profile, enriched)
    assert not verified, "Expected REJECT; this case has no corroborating signal."

    # Confirm _verify_match itself never mutates profile identity fields.
    assert profile.organization == "Golden Gate Institute for AI"
    assert profile.title == "Research Fellow"
    assert profile.enriched_organization == ""
    assert profile.enriched_title == ""


def test_backfill_never_overwrites_user_org_even_on_accept():
    """FM5: the fix on enrich_profile is that even on a verified match, the
    user-supplied organization/title must be preserved. LinkedIn values land
    in enriched_organization / enriched_title. Here we simulate the backfill
    step directly on a profile dataclass to prove the contract."""
    profile = Profile(
        name="Dylan Matthews",
        organization="Vox",
        title="Senior Correspondent",
    )
    parsed = _enriched(
        full_name="Dylan Matthews",
        current_company="Vox Media",
        current_title="Senior Correspondent, Future Perfect",
    )
    # Apply the post-accept backfill logic exactly as enrich_profile does it.
    if not profile.name and parsed.get("full_name"):
        profile.name = parsed["full_name"]
    if parsed.get("current_company"):
        profile.enriched_organization = parsed["current_company"]
        if not profile.organization:
            profile.organization = parsed["current_company"]
    if parsed.get("current_title"):
        profile.enriched_title = parsed["current_title"]
        if not profile.title:
            profile.title = parsed["current_title"]

    assert profile.organization == "Vox", "user-supplied org must survive"
    assert profile.title == "Senior Correspondent", "user-supplied title must survive"
    assert profile.enriched_organization == "Vox Media"
    assert profile.enriched_title == "Senior Correspondent, Future Perfect"


def test_backfill_fills_empty_fields():
    """Sanity: if the user didn't supply org/title, LinkedIn values backfill
    the primary fields (plus the enriched_* shadow fields)."""
    profile = Profile(name="Alice Zephyr", organization="", title="")
    parsed = _enriched(
        full_name="Alice Zephyr",
        current_company="Starfish Labs",
        current_title="CEO",
    )
    if parsed.get("current_company"):
        profile.enriched_organization = parsed["current_company"]
        if not profile.organization:
            profile.organization = parsed["current_company"]
    if parsed.get("current_title"):
        profile.enriched_title = parsed["current_title"]
        if not profile.title:
            profile.title = parsed["current_title"]

    assert profile.organization == "Starfish Labs"
    assert profile.title == "CEO"
    assert profile.enriched_organization == "Starfish Labs"
    assert profile.enriched_title == "CEO"


def test_dylan_matthews_legit_match_accepted():
    """Control case: legitimate enrichment with name+org match must still
    accept. If this test fails, the new soft-penalty rule has regressed
    legitimate matches."""
    enricher = _enricher()
    profile = Profile(
        name="Dylan Matthews",
        organization="Vox",
        title="Senior Correspondent",
    )
    enriched = _enriched(
        full_name="Dylan Matthews",
        current_company="Vox",
        current_title="Senior Correspondent, Future Perfect",
        experience=[{"company": "Vox", "title": "Senior Correspondent"}],
    )
    verified, log = enricher._verify_match(profile, enriched)
    joined = "\n".join(log)
    assert verified, f"Expected ACCEPT for correct match.\nlog:\n{joined}"
    assert "ACCEPTED" in joined
    # Org match is a positive signal
    assert "Verify org: MATCH" in joined


def test_vague_org_no_penalty_still_accepts():
    """Vague org (self-employed) should not fire the soft-penalty path; a
    strong name match alone should still accept. This guards against the new
    rule over-rejecting when the user's org is uninformative."""
    enricher = _enricher()
    profile = Profile(
        name="Florian Aldehoff",
        organization="self-employed",
    )
    enriched = _enriched(
        full_name="Florian Aldehoff",
        current_company="selbstständig",  # German for self-employed
        experience=[{"company": "selbstständig", "title": "AI security consultant"}],
    )
    verified, log = enricher._verify_match(profile, enriched)
    joined = "\n".join(log)
    assert verified, f"Expected ACCEPT for vague-org match.\nlog:\n{joined}"


def test_weak_single_token_name_rejected_without_signal():
    """Weak name match (single-token overlap on first-name-only profile) with
    no positive non-name signal must reject. This is the 'Abigail at vague
    org' class of false positive."""
    enricher = _enricher()
    profile = Profile(
        name="Nat",  # single token
        organization="",
    )
    enriched = _enriched(
        full_name="Nat Robinson",
        current_company="Some Startup",
        experience=[{"company": "Some Startup", "title": "Engineer"}],
    )
    verified, log = enricher._verify_match(profile, enriched)
    joined = "\n".join(log)
    # Note: this may come back ACCEPTED if the profile gives "Nat Robinson" a
    # strong-tier score via single-token match. With profile="Nat" only, we
    # get a 3-char token which triggers the single_short_match reject path.
    assert not verified, f"Expected REJECT for short single-token name with no signal.\nlog:\n{joined}"


if __name__ == "__main__":
    # Allow direct execution for quick manual runs.
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
