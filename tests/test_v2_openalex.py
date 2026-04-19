"""Tests for enrichment/v2/vertical_openalex.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import Profile
from enrichment.v2 import vertical_openalex
from enrichment.v2.cohort import classify_profile


def _mock_resp(payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    return m


def test_openalex_matches_on_name_and_institution() -> None:
    """Author with matching name + affiliation = strong evidence."""
    profile = Profile(name="Jane Doe", email="jane.doe@mit.edu")
    signals = classify_profile(profile)

    payload = {
        "results": [{
            "id": "https://openalex.org/A1234",
            "display_name": "Jane Doe",
            "works_count": 42,
            "cited_by_count": 300,
            "last_known_institution": {
                "display_name": "Massachusetts Institute of Technology",
                "homepage_url": "https://mit.edu",
                "ror": "https://ror.org/042nb2s44",
                "id": "https://openalex.org/I63966007",
            },
        }],
    }
    with patch.object(vertical_openalex.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_openalex.query_openalex(signals)

    assert len(r.hits) == 1
    ev = r.hits[0]
    assert "name_match" in ev.anchors
    assert "email_match" in ev.anchors
    assert "platform_match" in ev.anchors
    assert ev.is_strong()


def test_openalex_rejects_wrong_name() -> None:
    profile = Profile(name="Jane Doe", email="jane.doe@mit.edu")
    signals = classify_profile(profile)

    payload = {
        "results": [{
            "id": "https://openalex.org/A9999",
            "display_name": "Jane Smith",   # wrong last name
            "works_count": 10,
            "last_known_institution": {
                "display_name": "MIT",
                "homepage_url": "https://mit.edu",
            },
        }],
    }
    with patch.object(vertical_openalex.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_openalex.query_openalex(signals)

    assert r.hits == []


def test_openalex_name_match_only_is_still_returned() -> None:
    """Name-only hits are still Evidence, but with < 2 anchors so not strong."""
    profile = Profile(name="Jane Doe", email="jane.doe@gmail.com")
    # Note: gmail, so no org_domain. Only name_match should fire.
    signals = classify_profile(profile)

    payload = {
        "results": [{
            "id": "https://openalex.org/A5555",
            "display_name": "Jane Doe",
            "works_count": 5,
            "last_known_institution": {
                "display_name": "Unknown U",
                "homepage_url": "https://unknown.edu",
            },
        }],
    }
    with patch.object(vertical_openalex.requests, "get", return_value=_mock_resp(payload)):
        r = vertical_openalex.query_openalex(signals)

    assert len(r.hits) == 1
    # name_match + platform_match ≥ 2 — platform_match because the author has works
    assert r.hits[0].is_strong()


def test_openalex_api_error_graceful() -> None:
    profile = Profile(name="Jane Doe", email="jane@mit.edu")
    signals = classify_profile(profile)
    with patch.object(vertical_openalex.requests, "get", side_effect=Exception("network")):
        r = vertical_openalex.query_openalex(signals)
    assert r.hits == []


def test_openalex_skips_without_both_names() -> None:
    profile = Profile(name="Jane", email="jane@mit.edu")
    signals = classify_profile(profile)
    r = vertical_openalex.query_openalex(signals)
    assert r.queries == 0
    assert r.hits == []


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
