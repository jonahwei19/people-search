"""Tests for enrichment/v2/vertical_substack.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import Profile
from enrichment.v2 import vertical_substack
from enrichment.v2.cohort import classify_profile


def _mock_resp(payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    return m


def test_substack_matches_publication_by_author() -> None:
    profile = Profile(name="Matt Yglesias", email="mattyglesias@gmail.com")
    signals = classify_profile(profile)

    payload = {
        "results": [{
            "name": "Slow Boring",
            "base_url": "https://www.slowboring.com",
            "author_name": "Matt Yglesias",
            "description": "Matt Yglesias on politics and policy.",
        }],
    }
    with patch.object(vertical_substack.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_substack.query_substack(signals)

    assert len(r.hits) == 1
    ev = r.hits[0]
    assert "name_match" in ev.anchors
    assert "bio_match" in ev.anchors
    assert "platform_match" in ev.anchors


def test_substack_matches_by_slug_only() -> None:
    profile = Profile(name="Jane Doe", email="jane@example.com")
    signals = classify_profile(profile)

    payload = {
        "results": [{
            "name": "Something Else",
            "base_url": "https://jane-doe.substack.com",
            "author_name": "Unknown",
            "description": "A newsletter.",
        }],
    }
    with patch.object(vertical_substack.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_substack.query_substack(signals)

    assert len(r.hits) == 1
    ev = r.hits[0]
    assert "slug_match" in ev.anchors


def test_substack_ignores_irrelevant() -> None:
    profile = Profile(name="Jane Doe", email="jane@example.com")
    signals = classify_profile(profile)
    payload = {"results": [{
        "name": "Unrelated Newsletter",
        "base_url": "https://unrelated.substack.com",
        "author_name": "Bob Smith",
        "description": "About crypto.",
    }]}
    with patch.object(vertical_substack.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_substack.query_substack(signals)
    assert r.hits == []


def test_substack_graceful_on_error() -> None:
    profile = Profile(name="Jane Doe", email="jane@example.com")
    signals = classify_profile(profile)
    with patch.object(vertical_substack.requests, "get", side_effect=Exception("net")):
        r = vertical_substack.query_substack(signals)
    assert r.hits == []


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
