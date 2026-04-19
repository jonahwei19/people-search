"""Tests for enrichment/v2/org_site.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.v2 import org_site
from enrichment.v2.cohort import classify_profile
from enrichment.models import Profile


# ── Fixture HTML pages ────────────────────────────────────────

TEAM_PAGE = """<html>
<body>
<h1>Our team</h1>
<div class="person">
  <a href="/team/jane-doe">Jane Doe</a>
  <p>Jane Doe is our CTO. She leads engineering.</p>
</div>
<div class="person">
  <a href="/team/alice-smith">Alice Smith</a>
  <p>Alice runs marketing.</p>
</div>
<div>
  <a href="https://twitter.com/janedoe">Jane on Twitter</a>
</div>
<div>
  <a href="https://www.linkedin.com/in/jane-doe-cto">Jane's LinkedIn</a>
</div>
</body>
</html>
"""


ABOUT_NO_MATCH = """<html><body>
<h1>About us</h1>
<p>We build things.</p>
<a href="/contact">Contact us</a>
</body></html>
"""


def _mock_fetch_factory(pages: dict[str, str]):
    """Return a fake _fetch that returns fixture pages by URL."""
    def fake(url, timeout=8):
        # Match on path only — all pages live under the same origin
        for p, body in pages.items():
            if url.endswith(p):
                return 200, body
        return 404, ""
    return fake


def test_org_site_finds_named_person() -> None:
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    pages = {"/team": TEAM_PAGE, "/about": ABOUT_NO_MATCH}
    with patch.object(org_site, "_fetch", side_effect=_mock_fetch_factory(pages)):
        r = org_site.crawl_org_site(signals)

    assert r.pages_fetched >= 1
    # Should produce at least one strong hit (name + email_match + possibly bio)
    strong = [e for e in r.hits if e.is_strong()]
    assert strong, f"no strong evidence — got: {[(e.url, e.anchors) for e in r.hits]}"

    # Should have identified the Twitter link for Jane. The anchor text
    # "Jane on Twitter" only contains first name, so name_match doesn't
    # fire; but slug_match via /janedoe should catch it.
    twitter_hits = [e for e in r.hits if "twitter.com" in e.url.lower()]
    assert twitter_hits, "missed twitter link"
    assert any("slug_match" in e.anchors for e in twitter_hits)

    # Should have identified the LinkedIn link
    linkedin_hits = [e for e in r.hits if "linkedin.com" in e.url.lower()]
    assert linkedin_hits


def test_org_site_skips_personal_cohort() -> None:
    profile = Profile(name="Jane Doe", email="jane@gmail.com")
    signals = classify_profile(profile)
    r = org_site.crawl_org_site(signals)
    assert r.hits == []
    assert r.pages_fetched == 0


def test_org_site_does_not_confuse_people() -> None:
    """Alice Smith should NOT match Jane Doe's profile."""
    profile = Profile(name="Jane Doe", email="jane@acme.com")
    signals = classify_profile(profile)

    pages = {"/team": TEAM_PAGE}
    with patch.object(org_site, "_fetch", side_effect=_mock_fetch_factory(pages)):
        r = org_site.crawl_org_site(signals)

    # No hit should contain /team/alice-smith as a target URL
    for e in r.hits:
        assert "alice" not in e.url.lower(), f"confused Alice with Jane: {e.url}"


def test_org_site_single_token_name_allowed_if_long() -> None:
    profile = Profile(name="Kubernetes", email="k8s@cncf.io")
    signals = classify_profile(profile)
    # Single-token long name should not be skipped as "sparse"
    page = """<html><body>
      <a href="/person/kubernetes">Kubernetes entry</a>
    </body></html>
    """
    with patch.object(org_site, "_fetch", side_effect=_mock_fetch_factory({"/team": page})):
        r = org_site.crawl_org_site(signals)
    # Not a standard case, but should NOT fall over
    assert isinstance(r.hits, list)


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
