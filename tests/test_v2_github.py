"""Tests for enrichment/v2/vertical_github.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import Profile
from enrichment.v2 import vertical_github
from enrichment.v2.cohort import classify_profile


def _mock_resp(payload, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    return m


def _build_requests_mock(search_payload, user_payloads):
    """Build a side_effect function mapping URLs to payloads.

    NOTE: _mock_resp(..., status=404) still calls .json() in the caller, so
    we always return a 200 with empty payload for unknown URLs here.
    """
    def fake(url, params=None, headers=None, timeout=None):
        if "/search/users" in url:
            return _mock_resp(search_payload)
        for login, payload in user_payloads.items():
            if f"/users/{login}" in url:
                return _mock_resp(payload)
        return _mock_resp({}, status=404)
    return fake


def test_github_strong_match_on_name_bio_slug() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    search_payload = {"items": [{"login": "jane-doe"}]}
    user_payloads = {
        "jane-doe": {
            "login": "jane-doe",
            "name": "Jane Doe",
            "bio": "Jane Doe, CTO at Acme. Python, distributed systems.",
            "company": "Acme",
            "blog": "https://acme.com/jane",
            "html_url": "https://github.com/jane-doe",
            "public_repos": 25,
            "followers": 100,
        },
    }
    with patch.object(vertical_github.requests, "get",
                      side_effect=_build_requests_mock(search_payload, user_payloads)):
        r = vertical_github.query_github(signals)

    assert len(r.hits) == 1
    ev = r.hits[0]
    # Should have at least name_match, slug_match, email_match (acme in blog/company)
    assert "name_match" in ev.anchors
    assert "slug_match" in ev.anchors
    assert "email_match" in ev.anchors
    assert ev.is_strong()


def test_github_rejects_name_only_match() -> None:
    """A user with only name_match and no slug/bio/email is dropped."""
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)
    search_payload = {"items": [{"login": "someone-else"}]}
    user_payloads = {
        "someone-else": {
            "login": "someone-else",
            "name": "Jane Doe",       # name matches but nothing else
            "bio": "",
            "company": "",
            "blog": "",
            "html_url": "https://github.com/someone-else",
            "public_repos": 3,
        },
    }
    with patch.object(vertical_github.requests, "get",
                      side_effect=_build_requests_mock(search_payload, user_payloads)):
        r = vertical_github.query_github(signals)
    # Name only — rejected
    assert r.hits == []


def test_github_rejects_totally_wrong_name() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)
    search_payload = {"items": [{"login": "bob"}]}
    user_payloads = {
        "bob": {
            "login": "bob", "name": "Bob Smith", "bio": "", "html_url": "https://github.com/bob"
        },
    }
    with patch.object(vertical_github.requests, "get",
                      side_effect=_build_requests_mock(search_payload, user_payloads)):
        r = vertical_github.query_github(signals)
    assert r.hits == []


def test_github_search_error() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)
    with patch.object(vertical_github.requests, "get", side_effect=Exception("net")):
        r = vertical_github.query_github(signals)
    assert r.hits == []


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
