"""Tests for enrichment/v2/open_web.py — tight two-anchor fallback."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import Profile
from enrichment.v2 import open_web
from enrichment.v2.cohort import classify_profile


def test_open_web_requires_two_anchors() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    # Single-anchor hits (only name in snippet) should be filtered.
    results = [
        {
            "url": "https://some-random-site.org/jane-doe-profile",
            "title": "Jane Doe",
            "description": "Various Janes and Does.",
        },
    ]
    with patch.object(open_web, "_brave", return_value=results), \
         patch.object(open_web, "_serper", return_value=[]):
        r = open_web.query_open_web(
            signals, profile_org="Acme",
            brave_api_key="x", serper_api_key="y",
        )
    # name_match fires (title has "Jane Doe")
    # slug_match fires too (path contains "jane-doe")
    # → 2 anchors, retained
    assert len(r.hits) == 1
    assert r.hits[0].is_strong()


def test_open_web_rejects_single_anchor() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    results = [
        {
            # No slug match, no email domain, name only in snippet
            "url": "https://some-random-site.org/article-12345",
            "title": "Article featuring Jane Doe",
            "description": "About Jane Doe's work.",
        },
    ]
    with patch.object(open_web, "_brave", return_value=results), \
         patch.object(open_web, "_serper", return_value=[]):
        r = open_web.query_open_web(
            signals, profile_org="Acme",
            brave_api_key="x", serper_api_key="y",
        )
    # Only name_match → 1 anchor → rejected
    assert r.hits == []


def test_open_web_blocks_brokers() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    results = [{
        "url": "https://www.spokeo.com/jane-doe",
        "title": "Jane Doe — acme.com",
        "description": "Jane Doe at acme.com",
    }]
    with patch.object(open_web, "_brave", return_value=results), \
         patch.object(open_web, "_serper", return_value=[]):
        r = open_web.query_open_web(
            signals, profile_org="Acme",
            brave_api_key="x", serper_api_key="y",
        )
    assert r.hits == []


def test_open_web_org_domain_gives_email_anchor() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)
    results = [{
        "url": "https://acme.com/about/jane-doe",
        "title": "About Jane",
        "description": "Contact info.",
    }]
    with patch.object(open_web, "_brave", return_value=results), \
         patch.object(open_web, "_serper", return_value=[]):
        r = open_web.query_open_web(
            signals, profile_org="Acme",
            brave_api_key="x", serper_api_key="y",
        )
    assert len(r.hits) == 1
    # email_match (acme.com) + slug_match (jane-doe)
    assert "email_match" in r.hits[0].anchors
    assert "slug_match" in r.hits[0].anchors


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
