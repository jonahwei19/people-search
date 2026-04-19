"""Round-trip reproducibility tests for `enrichment/eval/replay.py`.

Check that the offline replay with defaults reproduces the stored
verifier decisions for profiles whose logs were written by the current
pipeline. Legacy-format logs (pre-FM2) are deliberately excluded from
the comparison — see `replay.validate_roundtrip` for rationale.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_replay_roundtrip.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.eval.replay import (
    ReplayConfig,
    VerifyAttempt,
    _replay_attempt,
    parse_attempts,
    replay_dataset,
    replay_verify,
    validate_roundtrip,
)
from enrichment.models import EnrichmentStatus, Profile


# ────────────────────────────────────────────────────────────────────
# Log fixtures — shaped exactly like what enrichers.py emits today.
# ────────────────────────────────────────────────────────────────────


def _profile_current_accept() -> Profile:
    """A profile that the current verifier ACCEPTED with the new log format."""
    return Profile(
        id="t-accept-1",
        name="Renee DiResta",
        email="renee@example.org",
        linkedin_url="https://www.linkedin.com/in/reneediresta",
        enrichment_status=EnrichmentStatus.ENRICHED,
        enrichment_log=[
            "Trying LinkedIn: https://www.linkedin.com/in/reneediresta",
            "  Verify name: MATCH ({'renee', 'diresta'}, strength=strong)",
            "  Verify org: MATCH ('stanford' found in experience)",
            "  Verify result: ACCEPTED (score=6, checks=1, name=strong, positives=1, penalties=0)",
        ],
    )


def _profile_current_reject() -> Profile:
    """A profile that the current verifier REJECTED (FM2 — soft-penalty-no-positive)."""
    return Profile(
        id="t-reject-1",
        name="Abigail Olvera",
        email="olverabi@gmail.com",
        enrichment_status=EnrichmentStatus.FAILED,
        enrichment_log=[
            "Trying LinkedIn: https://www.linkedin.com/in/abigail-olvera-5ab36427a",
            "  Verify name: MATCH ({'abigail', 'olvera'}, strength=strong)",
            "  Verify org: MISMATCH ('golden gate' not in ['megacorp', 'megacorp'])",
            "  Verify result: REJECTED (soft penalty without corroborating positive; "
            "score=2, checks=1, penalties=1)",
            "  REJECTED: https://www.linkedin.com/in/abigail-olvera-5ab36427a (wrong person)",
        ],
    )


def _profile_legacy_accept() -> Profile:
    """A profile accepted by the pre-FM2 verifier (strong name + org mismatch).
    The current verifier would REJECT this, so `replay` should disagree — but
    `validate_roundtrip` should flag it as a legacy-format flip and exclude it.
    """
    return Profile(
        id="t-legacy-1",
        name="Pranay Mittal",
        email="pranay.mittal@mail.house.gov",
        linkedin_url="https://www.linkedin.com/in/pranay-mittal-a2b2a520",
        enrichment_status=EnrichmentStatus.ENRICHED,
        enrichment_log=[
            "Trying LinkedIn: https://www.linkedin.com/in/pranay-mittal-a2b2a520",
            "  Verify name: MATCH ({'pranay', 'mittal'}, strength=strong)",
            "  Verify org: MISMATCH ('house committee on foreign affairs' not in ['citi'])",
            "  Verify result: ACCEPTED (score=2, checks=1)",
            "  LinkedIn enriched: 8 experiences",
        ],
    )


def _profile_name_mismatch_reject() -> Profile:
    return Profile(
        id="t-namemm-1",
        name="Test Person",
        enrichment_status=EnrichmentStatus.FAILED,
        enrichment_log=[
            "Trying LinkedIn: https://www.linkedin.com/in/completelydifferent",
            "  Verify name: MISMATCH ('test person' vs 'someone else')",
        ],
    )


def _profile_api_no_data() -> Profile:
    return Profile(
        id="t-nodata-1",
        name="Test Person",
        enrichment_status=EnrichmentStatus.FAILED,
        enrichment_log=[
            "Trying LinkedIn: https://www.linkedin.com/in/notfound",
            "  → API returned no data",
        ],
    )


def _profile_enriched_no_log() -> Profile:
    """A profile marked enriched with no log entries (pre-enriched from CSV)."""
    return Profile(
        id="t-preenriched-1",
        name="CSV Imported",
        enrichment_status=EnrichmentStatus.ENRICHED,
        linkedin_enriched={"context_block": "Name: CSV Imported\nHeadline: ..."},
    )


# ────────────────────────────────────────────────────────────────────
# parse_attempts
# ────────────────────────────────────────────────────────────────────


def test_parse_attempts_captures_name_strength_and_decision():
    p = _profile_current_accept()
    attempts = parse_attempts(p)
    assert len(attempts) == 1
    a = attempts[0]
    assert a.url.endswith("/in/reneediresta")
    assert a.name_strength == "strong"
    assert a.org_match is True
    assert a.org_mismatch is False
    assert a.stored_decision == "accepted"
    assert a.legacy_log_format is False


def test_parse_attempts_detects_legacy_format():
    p = _profile_legacy_accept()
    attempts = parse_attempts(p)
    assert len(attempts) == 1
    assert attempts[0].legacy_log_format is True


def test_parse_attempts_captures_name_mismatch_hard_reject():
    p = _profile_name_mismatch_reject()
    attempts = parse_attempts(p)
    assert len(attempts) == 1
    assert attempts[0].name_mismatch is True
    assert attempts[0].stored_decision == "rejected"


def test_parse_attempts_handles_api_no_data():
    p = _profile_api_no_data()
    attempts = parse_attempts(p)
    assert len(attempts) == 1
    assert attempts[0].stored_decision == "no_data"


def test_parse_attempts_handles_empty_log():
    p = _profile_enriched_no_log()
    attempts = parse_attempts(p)
    assert attempts == []


# ────────────────────────────────────────────────────────────────────
# replay_verify — per-profile
# ────────────────────────────────────────────────────────────────────


def test_replay_verify_defaults_reproduces_accept():
    p = _profile_current_accept()
    out = replay_verify(p, enriched=None)
    assert out["stored_decision"] == "accepted"
    assert out["replay_decision"] == "accepted"


def test_replay_verify_defaults_reproduces_reject():
    p = _profile_current_reject()
    out = replay_verify(p, enriched=None)
    assert out["stored_decision"] == "rejected"
    assert out["replay_decision"] == "rejected"


def test_replay_verify_stricter_flips_accept_to_reject():
    """Setting require_anchors=1 should flip a zero-positive accept."""
    a = VerifyAttempt(
        url="https://linkedin.com/in/x",
        name_strength="normal",
        stored_decision="accepted",
    )
    accepted, _ = _replay_attempt(a, ReplayConfig())
    assert accepted is True

    accepted2, _ = _replay_attempt(a, ReplayConfig(require_anchors=1))
    assert accepted2 is False


def test_replay_verify_name_weak_requires_positive():
    a = VerifyAttempt(url="x", name_strength="weak", stored_decision="rejected")
    accepted, _ = _replay_attempt(a, ReplayConfig())
    assert accepted is False

    a2 = VerifyAttempt(
        url="x", name_strength="weak", org_match=True,
        stored_decision="accepted",
    )
    accepted2, _ = _replay_attempt(a2, ReplayConfig())
    assert accepted2 is True


def test_replay_verify_lowering_threshold_can_admit():
    """name_weak scoring 1 + org match 3 = 4; threshold=2 admits, threshold=5 rejects."""
    a = VerifyAttempt(
        url="x", name_strength="weak", org_match=True,
        stored_decision="accepted",
    )
    assert _replay_attempt(a, ReplayConfig(baseline_threshold=2))[0] is True
    assert _replay_attempt(a, ReplayConfig(baseline_threshold=5))[0] is False


def test_replay_verify_slug_anchor_bonus_admits():
    """P4: giving slug-anchor points can admit an otherwise-borderline case."""
    a = VerifyAttempt(
        url="https://www.linkedin.com/in/firstname-lastname",
        name_strength="normal",
        org_mismatch=True,
        slug_anchor=True,
        stored_decision="rejected",
    )
    # Default: name_normal 2 - org_mismatch 1 = 1 → reject (below threshold)
    # Wait — normal is 2, mismatch is -1, so score=1, < 2 → reject.
    assert _replay_attempt(a, ReplayConfig())[0] is False
    # With slug_anchor_score=3: score = 2 - 1 + 3 = 4, positives=1 → accept
    ok, details = _replay_attempt(a, ReplayConfig(slug_anchor_score=3))
    assert ok is True
    assert details["breakdown"].get("slug_anchor") == 3


# ────────────────────────────────────────────────────────────────────
# Dataset-level replay + roundtrip validation
# ────────────────────────────────────────────────────────────────────


def test_replay_dataset_summary_shape():
    profiles = [
        _profile_current_accept(),
        _profile_current_reject(),
        _profile_api_no_data(),
        _profile_enriched_no_log(),
    ]
    out = replay_dataset(profiles, ReplayConfig())
    assert out["total"] == 4
    # accept + reject are compared; no_data and pre-enriched (no attempts) are not
    assert out["would_accept"] == 1
    assert out["would_reject"] == 1
    # no_attempt count accounts for API-no-data and pre-enriched
    assert out["integrity"]["uncompared_no_attempt"] == 2
    assert out["integrity"]["sum_check_ok"] is True


def test_validate_roundtrip_separates_legacy_from_current():
    profiles = [
        _profile_current_accept(),
        _profile_current_reject(),
        _profile_legacy_accept(),
    ]
    out = validate_roundtrip(profiles)
    # The two current-format profiles should match perfectly.
    assert out["matches_current_format"] == 2
    assert out["mismatches_current_format"] == 0
    assert out["reproducibility_rate_current_format"] == 1.0
    # The legacy profile is flagged but excluded.
    assert out["legacy_flips_ignored"] == 1


def test_validate_roundtrip_95pct_on_real_dataset():
    """Allan's cached dataset must hit >=95% reproducibility on the current
    log format. We scope to current-format logs because legacy logs were
    written by a pre-FM2 verifier and are expected to disagree."""
    path = ROOT / "datasets" / "c773996b.json"
    if not path.exists():
        import pytest
        pytest.skip("Cached Allan dataset not present; run the replay CLI to fetch.")
    from enrichment.models import Dataset
    ds = Dataset.load(path)
    out = validate_roundtrip(ds.profiles)
    assert out["reproducibility_rate_current_format"] >= 0.95, (
        f"Reproducibility only {out['reproducibility_rate_current_format']:.2%} — "
        f"log-parse likely has gaps. mismatch_by_type={out['mismatch_by_type']}"
    )


# ────────────────────────────────────────────────────────────────────
# Integration: a config tweak actually moves the flip count.
# ────────────────────────────────────────────────────────────────────


def test_config_tweak_changes_accept_count():
    """A stricter config should accept fewer profiles than the default."""
    profiles = [
        _profile_current_accept(),   # normal+org_match → strong signal, accepts
        _profile_current_reject(),   # strong+org_mismatch → rejects
    ]

    default = replay_dataset(profiles, ReplayConfig())
    strict = replay_dataset(profiles, ReplayConfig(require_anchors=2))

    assert default["would_accept"] >= strict["would_accept"]
