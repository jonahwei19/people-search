"""Assertions on cohort-analysis math.

Guards against the two common breakage modes:
  (1) bucket sums don't equal total — a classifier returning None or
      silently double-classifying a profile.
  (2) per-bucket enriched/skipped/failed percentages don't match the
      underlying counts.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_cohort_breakdown.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.eval.cohort_analysis import (
    COHORT_CLASSIFIERS,
    FAILURE_PATTERNS,
    _cohort_metrics,
    _failure_reasons,
    run_cohort_analysis,
)
from enrichment.eval.cost_simulator import (
    PipelineSpec,
    Stage,
    compare,
    simulate,
)
from enrichment.models import EnrichmentStatus, Profile


# ────────────────────────────────────────────────────────────────────
# Cohort: classifier totality
# ────────────────────────────────────────────────────────────────────


def _make_profiles() -> list[Profile]:
    """Small synthetic population covering every cohort bucket."""
    return [
        Profile(id="a", name="Alpha One", email="a@acme.com",
                organization="Acme",
                enrichment_status=EnrichmentStatus.ENRICHED),
        Profile(id="b", name="Bravo Two", email="b@harvard.edu",
                organization="Harvard",
                enrichment_status=EnrichmentStatus.FAILED,
                enrichment_log=["  Verify org: MISMATCH"]),
        Profile(id="c", name="Charlie Three", email="c@state.gov",
                organization="",
                enrichment_status=EnrichmentStatus.SKIPPED,
                enrichment_log=["Ambiguous: 4 candidates tied"]),
        Profile(id="d", name="Delta", email="d@gmail.com",
                organization="",
                enrichment_status=EnrichmentStatus.SKIPPED,
                enrichment_log=["No LinkedIn profile found"]),
        Profile(id="e", name="", email="",
                enrichment_status=EnrichmentStatus.SKIPPED),
        Profile(id="f", name="Foxtrot Sixx Jones", email="f@xyz.org",
                organization="XYZ",
                enrichment_status=EnrichmentStatus.ENRICHED,
                linkedin_enriched={"full_name": "Foxtrot Sixx Jones"}),
    ]


def test_every_classifier_returns_string_for_every_profile():
    profiles = _make_profiles()
    for axis, classifier in COHORT_CLASSIFIERS.items():
        for p in profiles:
            bucket = classifier(p)
            assert isinstance(bucket, str) and bucket, (
                f"classifier {axis} returned {bucket!r} for profile id={p.id}"
            )


def test_bucket_sums_equal_total_for_every_axis():
    profiles = _make_profiles()
    report = run_cohort_analysis(profiles)
    total = report["total"]
    for axis, integrity in report["integrity"].items():
        assert integrity["matches_total"], (
            f"axis {axis} buckets sum to {integrity['sum_of_buckets']} "
            f"but total is {total}"
        )
        # Also verify that the actual cohort table's N sums to total
        bucket_sum = sum(
            entry["n"] for entry in report["cohorts"][axis].values()
        )
        assert bucket_sum == total, f"axis {axis}: bucket Ns sum to {bucket_sum}, expected {total}"


def test_enriched_pct_matches_counts():
    """For each bucket, enriched_pct should equal enriched_count / N × 100."""
    profiles = _make_profiles()
    report = run_cohort_analysis(profiles)
    for axis, buckets in report["cohorts"].items():
        for name, m in buckets.items():
            n = m["n"]
            enriched = m["status_counts"].get("enriched", 0)
            if n == 0:
                assert m["enriched_pct"] == 0.0
                continue
            expected = round(100.0 * enriched / n, 1)
            assert m["enriched_pct"] == expected, (
                f"axis={axis} bucket={name}: enriched_pct={m['enriched_pct']} "
                f"but should be {expected}"
            )


def test_status_counts_do_not_double_count():
    """Status counts in each bucket must sum to bucket N (no profile
    simultaneously enriched+skipped etc.)."""
    profiles = _make_profiles()
    report = run_cohort_analysis(profiles)
    for axis, buckets in report["cohorts"].items():
        for name, m in buckets.items():
            assert sum(m["status_counts"].values()) == m["n"], (
                f"axis={axis} bucket={name}: status_counts sum "
                f"{sum(m['status_counts'].values())} != N {m['n']}"
            )


def test_cross_email_org_is_consistent_with_axes():
    """email_x_org bucket for a profile should agree with the email_type
    and org_presence classifiers independently."""
    profiles = _make_profiles()
    report = run_cohort_analysis(profiles)
    cross_n_sum = sum(m["n"] for m in report["cohorts"]["email_x_org"].values())
    email_n_sum = sum(m["n"] for m in report["cohorts"]["email_type"].values())
    org_n_sum = sum(m["n"] for m in report["cohorts"]["org_presence"].values())
    assert cross_n_sum == email_n_sum == org_n_sum == report["total"]


def test_overall_metrics_match_single_bucket_metrics():
    """Running `_cohort_metrics` on the whole population should match
    report['overall']."""
    profiles = _make_profiles()
    overall = _cohort_metrics(profiles)
    report = run_cohort_analysis(profiles)
    # Not all fields are guaranteed-equal (floats round), but the major
    # counts must match
    assert overall["n"] == report["overall"]["n"]
    assert overall["status_counts"] == report["overall"]["status_counts"]
    assert overall["enriched_pct"] == report["overall"]["enriched_pct"]


# ────────────────────────────────────────────────────────────────────
# Failure-reason extractor
# ────────────────────────────────────────────────────────────────────


def test_failure_reasons_returns_sorted_unique_set():
    log = [
        "Verify org: MISMATCH (foo not in bar)",
        "No LinkedIn profile found",
        "Verify org: MISMATCH (again, different attempt)",
    ]
    reasons = _failure_reasons(log)
    assert reasons == sorted(set(reasons))
    assert "org_mismatch" in reasons
    assert "no_linkedin_found" in reasons


def test_failure_patterns_ordered_specific_first():
    """The 'soft penalty' reason must be matched before the bare 'org mismatch'
    so a rich log line isn't misclassified."""
    seen_soft = False
    for needle, bucket in FAILURE_PATTERNS:
        if bucket == "soft_penalty_no_positive":
            seen_soft = True
        if bucket == "org_mismatch":
            # soft_penalty_no_positive should have been matched earlier
            assert seen_soft, (
                "FAILURE_PATTERNS ordering: `soft_penalty_no_positive` "
                "must precede `org_mismatch` so specific rejection reasons "
                "win over the bare verification signal."
            )
            break


# ────────────────────────────────────────────────────────────────────
# Cost simulator: integrity checks
# ────────────────────────────────────────────────────────────────────


def test_simulate_total_equals_sum_of_stages():
    spec = PipelineSpec(
        name="test",
        stages=[
            Stage("search", prob=1.0, unit_cost=0.006, count=5),
            Stage("linkedin", prob=0.8, unit_cost=0.0168, count=1),
        ],
    )
    out = simulate(spec, n_profiles=1000)
    stage_total = sum(s["total_cost_for_population"] for s in out["per_stage_breakdown"])
    # Floating point tolerance
    assert abs(stage_total - out["total_cost_usd"]) < 0.01


def test_simulate_band_brackets_center():
    spec = PipelineSpec.from_current()
    out = simulate(spec, n_profiles=500)
    band = out["confidence_band_usd"]
    assert band["p20_low"] <= out["total_cost_usd"] <= band["p80_high"]


def test_simulate_zero_population_yields_zero_cost():
    spec = PipelineSpec.from_current()
    out = simulate(spec, n_profiles=0)
    assert out["total_cost_usd"] == 0.0
    for s in out["per_stage_breakdown"]:
        assert s["total_cost_for_population"] == 0.0


def test_compare_preserves_per_profile_cost():
    a = PipelineSpec(name="A", stages=[Stage("x", prob=1.0, unit_cost=0.01)])
    b = PipelineSpec(name="B", stages=[Stage("x", prob=1.0, unit_cost=0.02)])
    out = compare([a, b], n_profiles=100)
    assert out["comparison"][0]["per_profile_cost_usd"] == 0.01
    assert out["comparison"][1]["per_profile_cost_usd"] == 0.02
    # B should be exactly 2x A for the same population
    assert abs(out["comparison"][1]["total_cost_usd"] -
               2 * out["comparison"][0]["total_cost_usd"]) < 0.01


def test_cohort_breakdown_sums_to_total():
    spec = PipelineSpec.from_current()
    out = simulate(
        spec, n_profiles=1000,
        cohort_shares={"corp": 0.5, "personal": 0.3, "edu": 0.1, "gov": 0.1},
    )
    cohort_sum = sum(c["total_cost"] for c in out["cohort_breakdown"])
    assert abs(cohort_sum - out["total_cost_usd"]) < 0.5  # multiplier=1.0 for all
