"""Structured-observability tests for verification_decisions (P5).

Covers:
- `_verify_match` records one decision per candidate pass with the
  expected shape.
- Multiple call sites (reject / accept paths) all emit a record.
- Anchors are tagged correctly for positive and negative signals.
- Supabase storage serializes / deserializes the field end-to-end.
- coverage_report aggregates reason categories correctly.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_verification_decisions.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.enrichers import LinkedInEnricher
from enrichment.eval.coverage_report import run_report, _normalize_decision_reason
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


EXPECTED_KEYS = {
    "linkedin_url",
    "enriched_name",
    "score",
    "anchors_positive",
    "anchors_negative",
    "decision",
    "reason",
    "timestamp",
}


def test_accept_decision_shape():
    """Accept path records a decision with all expected keys."""
    enricher = _enricher()
    profile = Profile(name="Dylan Matthews", organization="Vox")
    enriched = _enriched(
        full_name="Dylan Matthews",
        current_company="Vox",
        experience=[{"company": "Vox", "title": "Senior Correspondent"}],
    )
    url = "https://www.linkedin.com/in/dylan-matthews-1234"
    verified, _ = enricher._verify_match(profile, enriched, url)
    assert verified
    assert len(profile.verification_decisions) == 1
    record = profile.verification_decisions[0]
    assert EXPECTED_KEYS <= set(record.keys()), f"missing keys: {EXPECTED_KEYS - set(record.keys())}"
    assert record["decision"] == "accept"
    assert record["linkedin_url"] == url
    assert record["enriched_name"] == "dylan matthews"  # normalized
    assert record["score"] >= 2
    # Expected positive anchors
    assert "name_strong" in record["anchors_positive"]
    assert "org_match" in record["anchors_positive"]
    assert "slug_match" in record["anchors_positive"]
    # No negatives
    assert record["anchors_negative"] == []
    # ISO timestamp
    assert "T" in record["timestamp"]


def test_reject_name_mismatch_records_decision():
    """Name-mismatch reject path emits a structured record too."""
    enricher = _enricher()
    profile = Profile(name="Alice Zephyr", organization="Starfish Labs")
    enriched = _enriched(
        full_name="Bob Smith",  # totally different name
        current_company="Something Else",
    )
    url = "https://www.linkedin.com/in/bob-smith"
    verified, _ = enricher._verify_match(profile, enriched, url)
    assert not verified
    assert len(profile.verification_decisions) == 1
    record = profile.verification_decisions[0]
    assert record["decision"] == "reject"
    assert "name_mismatch" in record["anchors_negative"]


def test_reject_soft_penalty_records_decision():
    """Name match + org mismatch with no corroboration → reject record with
    org_mismatch in anchors_negative."""
    enricher = _enricher()
    profile = Profile(
        name="Abigail Olvera",
        organization="Golden Gate Institute for AI",
    )
    enriched = _enriched(
        full_name="Abigail Olvera",
        current_company="MegaCorp",
        experience=[{"company": "MegaCorp", "title": "Property Manager"}],
    )
    url = "https://www.linkedin.com/in/numeric-slug-9876"
    verified, _ = enricher._verify_match(profile, enriched, url)
    assert not verified
    assert profile.verification_decisions
    record = profile.verification_decisions[-1]
    assert record["decision"] == "reject"
    assert "org_mismatch" in record["anchors_negative"]
    # name_strong still fired even though we ultimately rejected
    assert "name_strong" in record["anchors_positive"]


def test_multiple_candidates_append_multiple_decisions():
    """Running _verify_match twice in succession (simulating the multi-URL
    retry loop) appends BOTH records to the same profile."""
    enricher = _enricher()
    profile = Profile(name="Renee DiResta", organization="Stanford")
    wrong = _enriched(
        full_name="Bob Wrong",
        current_company="Not Stanford",
    )
    correct = _enriched(
        full_name="Renee DiResta",
        current_company="Stanford Internet Observatory",
        experience=[{"company": "Stanford", "title": "Research Manager"}],
    )
    enricher._verify_match(profile, wrong, "https://linkedin.com/in/bob-wrong")
    enricher._verify_match(profile, correct, "https://linkedin.com/in/renee-diresta")
    assert len(profile.verification_decisions) == 2
    assert profile.verification_decisions[0]["decision"] == "reject"
    assert profile.verification_decisions[1]["decision"] == "accept"


def test_supabase_roundtrip_preserves_verification_decisions():
    """Mocked Supabase storage roundtrip keeps verification_decisions intact."""
    from cloud.storage.supabase import SupabaseStorage

    # Build a profile with a decision already populated.
    profile = Profile(
        id="test1234",
        name="Test Person",
        email="test@example.com",
        linkedin_url="https://linkedin.com/in/test-person",
    )
    profile.verification_decisions = [
        {
            "linkedin_url": "https://linkedin.com/in/test-person",
            "enriched_name": "test person",
            "score": 5,
            "anchors_positive": ["name_strong", "slug_match"],
            "anchors_negative": [],
            "decision": "accept",
            "reason": "accepted (name=strong, positives=2, penalties=0)",
            "timestamp": "2026-04-19T12:00:00+00:00",
        }
    ]

    # Build a SupabaseStorage instance but skip real client creation.
    with patch("cloud.storage.supabase.create_client") as mk:
        mk.return_value = MagicMock()
        storage = SupabaseStorage(
            supabase_url="https://fake.supabase.co",
            supabase_key="fake-key",
            account_id="account-123",
        )

    # _profile_to_row should serialize the field.
    row = storage._profile_to_row(profile, dataset_id="ds1")
    assert "verification_decisions" in row, f"row keys={list(row)}"
    assert row["verification_decisions"] == profile.verification_decisions

    # _row_to_profile should deserialize.
    round_trip = storage._row_to_profile(row)
    assert round_trip.verification_decisions == profile.verification_decisions

    # And NULL / missing column should yield [] (backwards-compat).
    row_no_col = dict(row)
    row_no_col.pop("verification_decisions")
    empty_profile = storage._row_to_profile(row_no_col)
    assert empty_profile.verification_decisions == []


def test_coverage_report_breaks_down_by_reason():
    """run_report aggregates verification_decisions across profiles into the
    expected slice."""
    p1 = Profile(name="A")
    p1.verification_decisions = [
        {
            "linkedin_url": "url1",
            "enriched_name": "a a",
            "score": 5,
            "anchors_positive": ["name_strong", "slug_match"],
            "anchors_negative": [],
            "decision": "accept",
            "reason": "accepted (name=strong, positives=2)",
            "timestamp": "2026-04-19T12:00:00Z",
        }
    ]
    p2 = Profile(name="B")
    p2.verification_decisions = [
        {
            "linkedin_url": "url2",
            "enriched_name": "b b",
            "score": 2,
            "anchors_positive": ["name_normal"],
            "anchors_negative": ["org_mismatch"],
            "decision": "reject",
            "reason": "soft_penalty_no_positive (score=2, penalties=1)",
            "timestamp": "2026-04-19T12:00:00Z",
        },
        {
            "linkedin_url": "url3",
            "enriched_name": "b other",
            "score": 1,
            "anchors_positive": ["name_weak"],
            "anchors_negative": [],
            "decision": "reject",
            "reason": "score_below_threshold (score=1, checks=0)",
            "timestamp": "2026-04-19T12:01:00Z",
        },
    ]
    report = run_report([p1, p2])
    vd = report["verification_decisions"]
    assert vd["decision_counts"] == {"accept": 1, "reject": 2}
    assert vd["reason_counts"]["accept:accepted"] == 1
    assert vd["reason_counts"]["reject:soft_penalty_no_positive"] == 1
    assert vd["reason_counts"]["reject:score_below_threshold"] == 1
    assert vd["anchors_positive_counts"]["name_strong"] == 1
    assert vd["anchors_positive_counts"]["name_normal"] == 1
    assert vd["anchors_positive_counts"]["slug_match"] == 1
    assert vd["anchors_negative_counts"]["org_mismatch"] == 1


def test_reason_normalizer_buckets_variants():
    assert _normalize_decision_reason("score_below_threshold (score=1)") == "score_below_threshold"
    assert _normalize_decision_reason(
        "weak name match rejected (last-name-missing)"
    ) == "weak_match_last_name_missing"
    assert _normalize_decision_reason("arbiter selected index=1") == "arbiter_selected"
    assert _normalize_decision_reason("") == "unspecified"
    assert _normalize_decision_reason("something entirely new") == "other"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
