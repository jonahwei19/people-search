"""Tests for enrichment/v2/evidence.py."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.v2.evidence import Evidence, merge_evidence, strong_anchors_count


def _ev(url, source="org_site", anchors=None, snippet="", kind="listing"):
    e = Evidence(url=url, source=source, kind=kind, snippet=snippet)
    for a in anchors or []:
        e.add_anchor(a)
    return e


def test_evidence_strength_threshold() -> None:
    e = _ev("https://x.com", anchors=["name_match"])
    assert not e.is_strong()
    e.add_anchor("email_match")
    assert e.is_strong()


def test_invalid_anchor_rejected() -> None:
    e = _ev("https://x.com")
    try:
        e.add_anchor("not_a_real_anchor")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown anchor type")


def test_merge_evidence_unions_anchors() -> None:
    a = [_ev("https://mit.edu/~jd", source="org_site", anchors=["email_match"])]
    b = [_ev("https://mit.edu/~jd", source="open_web", anchors=["name_match"])]
    merged = merge_evidence(a, b)
    assert len(merged) == 1
    assert merged[0].anchors == {"email_match", "name_match"}
    assert merged[0].is_strong()


def test_merge_evidence_distinct_urls() -> None:
    a = [_ev("https://a.edu", anchors=["email_match"])]
    b = [_ev("https://b.edu", anchors=["name_match"])]
    merged = merge_evidence(a, b)
    assert len(merged) == 2


def test_strong_anchors_count() -> None:
    pile = [
        _ev("https://a", anchors=["name_match", "email_match"]),
        _ev("https://b", anchors=["name_match"]),
        _ev("https://c", anchors=["name_match", "slug_match", "bio_match"]),
    ]
    assert strong_anchors_count(pile) == 2


def test_canonicalization_on_merge() -> None:
    """URLs with trailing slash / different case should merge."""
    a = [_ev("https://MIT.edu/~jd/", anchors=["email_match"])]
    b = [_ev("https://mit.edu/~jd", anchors=["name_match"])]
    merged = merge_evidence(a, b)
    assert len(merged) == 1
    assert merged[0].anchors == {"email_match", "name_match"}


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
