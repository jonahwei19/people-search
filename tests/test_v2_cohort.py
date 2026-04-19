"""Tests for enrichment/v2/cohort.py."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import Profile
from enrichment.v2 import cohort


def test_classify_email_personal() -> None:
    c, dom = cohort.classify_email("jane@gmail.com")
    assert c == cohort.COHORT_PERSONAL
    assert dom == ""


def test_classify_email_edu() -> None:
    c, dom = cohort.classify_email("jane@cs.mit.edu")
    assert c == cohort.COHORT_EDU
    assert dom == "cs.mit.edu"


def test_classify_email_gov() -> None:
    c, dom = cohort.classify_email("j.doe@state.gov")
    assert c == cohort.COHORT_GOV
    assert dom == "state.gov"


def test_classify_email_corp() -> None:
    c, dom = cohort.classify_email("alice@stripe.com")
    assert c == cohort.COHORT_CORP
    assert dom == "stripe.com"


def test_classify_email_org() -> None:
    c, dom = cohort.classify_email("bob@aclu.org")
    assert c == cohort.COHORT_ORG
    assert dom == "aclu.org"


def test_classify_email_missing() -> None:
    c, dom = cohort.classify_email("")
    assert c == cohort.COHORT_NO_EMAIL
    assert dom == ""


def test_generate_name_slugs_orders_distinctive_first() -> None:
    slugs = cohort.generate_name_slugs("Jane Doe")
    # first-last should be #1, firstlast #2
    assert slugs[0] == "jane-doe"
    assert "janedoe" in slugs
    # must include common professional patterns
    assert "jdoe" in slugs  # initial+last, special allowance for this pattern


def test_generate_name_slugs_strips_credentials() -> None:
    slugs = cohort.generate_name_slugs("Jane Doe, Ph.D.")
    assert "jane-doe" in slugs
    # no slug should contain 'phd' or 'ph.d.'
    assert not any("phd" in s for s in slugs)


def test_generate_name_slugs_single_token() -> None:
    slugs = cohort.generate_name_slugs("Cher")
    # 4 chars, too short — no slug
    assert slugs == []
    slugs = cohort.generate_name_slugs("Rihanna")
    assert "rihanna" in slugs


def test_classify_profile_end_to_end() -> None:
    p = Profile(name="Jane Doe", email="jane@mit.edu")
    sig = cohort.classify_profile(p)
    assert sig.cohort == cohort.COHORT_EDU
    assert sig.org_domain == "mit.edu"
    assert sig.first == "jane"
    assert sig.last == "doe"
    assert "jane-doe" in sig.name_slugs


def test_slug_matches_url() -> None:
    slugs = cohort.generate_name_slugs("Jane Doe")
    assert cohort.slug_matches_url(slugs, "https://mit.edu/team/jane-doe") == "jane-doe"
    assert cohort.slug_matches_url(slugs, "https://example.com/author/janedoe") == "janedoe"
    assert cohort.slug_matches_url(slugs, "https://example.com/author/unrelated") == ""


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
