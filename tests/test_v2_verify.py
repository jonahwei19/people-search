"""Tests for enrichment/v2/verify.py — two-anchor rule + profile writer."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment.models import EnrichmentStatus, Profile
from enrichment.v2.evidence import Evidence
from enrichment.v2.verify import (
    STATE_ENRICHED, STATE_HIDDEN, STATE_THIN, verify, write_profile,
)


def _ev(url, anchors, source="test", kind="listing", snippet=""):
    e = Evidence(url=url, source=source, kind=kind, snippet=snippet)
    for a in anchors:
        e.add_anchor(a)
    return e


# ── verify() ────────────────────────────────────────────────

def test_verify_strong_single_hit_enriched() -> None:
    ev = [_ev("https://x", ["name_match", "email_match"])]
    r = verify(ev)
    assert r.state == STATE_ENRICHED
    assert len(r.strong_evidence) == 1


def test_verify_two_weak_distinct_sources_enriched() -> None:
    ev = [
        _ev("https://a", ["name_match"], source="org_site"),
        _ev("https://b", ["name_match"], source="openalex"),
    ]
    r = verify(ev)
    assert r.state == STATE_ENRICHED


def test_verify_two_weak_same_source_thin() -> None:
    ev = [
        _ev("https://a", ["name_match"], source="org_site"),
        _ev("https://b", ["name_match"], source="org_site"),
    ]
    r = verify(ev)
    # Same source: doesn't cross-verify. Still thin.
    assert r.state == STATE_THIN


def test_verify_one_weak_thin() -> None:
    ev = [_ev("https://a", ["name_match"])]
    r = verify(ev)
    assert r.state == STATE_THIN


def test_verify_empty_hidden() -> None:
    r = verify([])
    assert r.state == STATE_HIDDEN


# ── write_profile() ─────────────────────────────────────────

def test_write_profile_does_not_overwrite_user_org() -> None:
    p = Profile(name="Jane Doe", email="jane@acme.com",
                organization="Acme Inc", title="CEO")
    ev = [_ev("https://linkedin.com/in/jane-doe",
              ["name_match", "email_match"], kind="linkedin")]
    r = verify(ev)
    enriched_data = {
        "full_name": "Jane Doe",
        "current_company": "Globex Corp",
        "current_title": "CTO",
        "context_block": "Jane Doe at Globex",
    }
    write_profile(p, r, enriched_data=enriched_data)

    # User-provided values preserved
    assert p.organization == "Acme Inc"
    assert p.title == "CEO"
    # Enriched values stored separately
    assert p.enriched_organization == "Globex Corp"
    assert p.enriched_title == "CTO"
    assert p.enrichment_status == EnrichmentStatus.ENRICHED


def test_write_profile_fills_empty_org() -> None:
    p = Profile(name="Jane Doe", email="jane@acme.com")  # no org / title
    ev = [_ev("https://linkedin.com/in/jane-doe",
              ["name_match", "email_match"], kind="linkedin")]
    r = verify(ev)
    enriched_data = {
        "full_name": "Jane Doe",
        "current_company": "Acme Corp",
        "current_title": "CTO",
        "context_block": "...",
    }
    write_profile(p, r, enriched_data=enriched_data)

    assert p.organization == "Acme Corp"
    assert p.title == "CTO"
    assert p.enriched_organization == "Acme Corp"
    assert p.enriched_title == "CTO"


def test_write_profile_sets_website_from_strong_bio() -> None:
    p = Profile(name="Jane Doe", email="jane@acme.com")
    ev = [_ev("https://acme.com/team/jane",
              ["name_match", "email_match", "bio_match"],
              kind="bio", snippet="Jane Doe is our CTO.")]
    r = verify(ev)
    write_profile(p, r)
    assert p.website_url == "https://acme.com/team/jane"
    # Snippet should end up in fetched_content under strong-confidence key
    assert any("strong" in k for k in p.fetched_content.keys())


def test_write_profile_hidden_marks_skipped() -> None:
    p = Profile(name="Jane Doe", email="jane@acme.com")
    r = verify([])
    write_profile(p, r)
    assert p.enrichment_status == EnrichmentStatus.SKIPPED


def test_write_profile_multiple_kinds_populate_fields() -> None:
    p = Profile(name="Jane Doe", email="jane@acme.com")
    ev = [
        _ev("https://acme.com/team/jane-doe",
            ["name_match", "email_match"], kind="bio"),
        _ev("https://twitter.com/janedoe",
            ["slug_match", "email_match"], kind="twitter"),
        _ev("https://github.com/jane-doe",
            ["name_match", "slug_match"], kind="github"),
    ]
    r = verify(ev)
    write_profile(p, r)
    assert p.website_url == "https://acme.com/team/jane-doe"
    assert p.twitter_url == "https://twitter.com/janedoe"
    assert "https://github.com/jane-doe" in p.other_links


if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v"], check=False)
