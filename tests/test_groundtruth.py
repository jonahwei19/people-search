"""Tests for ground-truth CSV loader + scoring."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from enrichment.eval.groundtruth import GroundTruthEntry, load_groundtruth, score_against
from enrichment.models import Profile, EnrichmentStatus


def _write_gt_csv(rows: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=[
        "profile_id", "uploaded_name", "email", "organization", "title",
        "current_linkedin_url", "current_enrichment_status",
        "true_linkedin_url", "true_website_url", "true_twitter_url",
        "true_is_hidden", "notes",
    ])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "profile_id": r["id"],
            "uploaded_name": r.get("name", ""),
            "email": r.get("email", ""),
            "organization": "",
            "title": "",
            "current_linkedin_url": "",
            "current_enrichment_status": "",
            "true_linkedin_url": r.get("true_linkedin", ""),
            "true_website_url": r.get("true_website", ""),
            "true_twitter_url": r.get("true_twitter", ""),
            "true_is_hidden": r.get("true_is_hidden", ""),
            "notes": "",
        })
    f.close()
    return Path(f.name)


def test_load_groundtruth_roundtrip():
    p = _write_gt_csv([
        {"id": "a1", "name": "Alice", "email": "a@x.com", "true_linkedin": "https://linkedin.com/in/alice"},
        {"id": "b2", "name": "Bob", "email": "b@x.com", "true_is_hidden": "true"},
    ])
    try:
        gt = load_groundtruth(p)
        assert len(gt) == 2
        ids = {e.profile_id for e in gt}
        assert ids == {"a1", "b2"}
        alice = next(e for e in gt if e.profile_id == "a1")
        assert alice.true_linkedin_url == "https://linkedin.com/in/alice"
        bob = next(e for e in gt if e.profile_id == "b2")
        assert bob.true_is_hidden is True
    finally:
        p.unlink()


def test_score_against_perfect_match():
    gt = [GroundTruthEntry(profile_id="a1", uploaded_name="Alice", email="", true_linkedin_url="https://linkedin.com/in/alice")]
    p = Profile(id="a1", name="Alice", linkedin_url="https://linkedin.com/in/alice", enrichment_status=EnrichmentStatus.ENRICHED)
    m = score_against([p], gt)
    assert m["linkedin_precision"] == 1.0
    assert m["linkedin_recall"] == 1.0
    assert m["linkedin_f1"] == 1.0
    assert m["linkedin_wrong_person"] == 0


def test_score_against_wrong_person():
    gt = [GroundTruthEntry(profile_id="a1", uploaded_name="Alice", email="", true_linkedin_url="https://linkedin.com/in/real-alice")]
    p = Profile(id="a1", name="Alice", linkedin_url="https://linkedin.com/in/different-alice", enrichment_status=EnrichmentStatus.ENRICHED)
    m = score_against([p], gt)
    assert m["linkedin_wrong_person"] == 1
    assert m["linkedin_precision"] == 0.0


def test_score_against_miss():
    gt = [GroundTruthEntry(profile_id="a1", uploaded_name="Alice", email="", true_linkedin_url="https://linkedin.com/in/alice")]
    p = Profile(id="a1", name="Alice", linkedin_url="", enrichment_status=EnrichmentStatus.FAILED)
    m = score_against([p], gt)
    assert m["linkedin_fn"] == 1
    assert m["linkedin_recall"] == 0.0


def test_score_against_correct_negative():
    """Hidden person, we correctly didn't attribute — should be a TN not a miss."""
    gt = [GroundTruthEntry(profile_id="a1", uploaded_name="Alice", email="", true_is_hidden=True)]
    p = Profile(id="a1", name="Alice", linkedin_url="", enrichment_status=EnrichmentStatus.SKIPPED)
    m = score_against([p], gt)
    assert m["linkedin_tn"] == 1
    assert m["hidden_accuracy"] == 1.0
