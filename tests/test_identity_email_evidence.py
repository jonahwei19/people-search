"""Regression tests for the `_email_evidence` scoping bug + the dead
`_follow_email_evidence` code path in `enrichment/identity.py`.

Context
-------
The bug (FM1 in plans/diagnosis_correctness.md and P1 in
plans/diagnosis_hitrate.md) was that `resolve_profile` ran a broad
`"<email_username>" linkedin OR researchgate` query and tagged EVERY
returned LinkedIn with `_email_evidence=True`. In `_score_candidates`
that flag was worth +20, so for common first-name locals (dan, ari,
evan, john, milica, colin) the pool filled up with 5–10 unrelated
LinkedIns all tied at score ~23 — which drove ambiguous-tie skips and
wrong-person matches (the Abi Olvera → Naji Abi-Hashem case started
here).

Secondary bug: `_follow_email_evidence()` was dead code because
`search()` filtered to LinkedIn-only before returning, so the
non-LinkedIn pages it was supposed to follow never reached it.

What this test checks
---------------------
1. LinkedIn candidates from the `email-exact` search ARE tagged as
   email evidence (type=exact, +20 scoring bonus).
2. LinkedIn candidates from the `email-username` search are NOT
   tagged as email evidence and receive no +20 bonus.
3. The pathological score 23 tie (the telltale signature of the
   pre-fix bug) does NOT occur for a common-first-name email.
4. `_follow_email_evidence` actually runs on non-LinkedIn results
   returned by the email-exact search. The safe-domain gate filters
   out known brokers so we don't blow up the query budget.

Run
---
    cd candidate-search-tool/
    python tests/test_identity_email_evidence.py
    # or
    python -m pytest tests/test_identity_email_evidence.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enrichment import identity  # noqa: E402
from enrichment.identity import IdentityResolver  # noqa: E402
from enrichment.models import Profile  # noqa: E402


# ── Fake web-search fixtures ─────────────────────────────────────────

def _fake_web_search_factory():
    """Build a fake `_web_search` that returns different results per query.

    Keyed by substrings that appear in the query string. Order matters —
    we match on the first substring that hits, so put more specific
    strings first.
    """

    # email-exact — literal "{email}" query. The one LinkedIn candidate
    # here is the REAL person. Also returns a non-LinkedIn page on the
    # profile's own email domain so `_follow_email_evidence` has
    # something to work with.
    email_exact_results = [
        {
            "title": "John Smith - Corp Inc",
            "url": "https://www.linkedin.com/in/johnsmith-corp",
            "description": "John Smith at Corp Inc. john@corp.com",
        },
        {
            "title": "Our team — Corp Inc",
            "url": "https://corp.com/team/john-smith",
            "description": "John Smith leads engineering. Contact: john@corp.com",
        },
    ]

    # email-username — broad "{email_local}" linkedin OR researchgate.
    # Returns five unrelated Johns — this is the pathological case
    # that used to all score 23 pre-fix.
    email_username_results = [
        {
            "title": "John Baker – Baker Industries",
            "url": "https://www.linkedin.com/in/john-baker-unrelated",
            "description": "John Baker at Baker Industries, wholly unrelated.",
        },
        {
            "title": "John Chen – AcmeCo",
            "url": "https://www.linkedin.com/in/johnchen-acme",
            "description": "John Chen, Product @ AcmeCo",
        },
        {
            "title": "John Davidson – Alpha",
            "url": "https://www.linkedin.com/in/john-davidson-alpha",
            "description": "John Davidson, VP at Alpha",
        },
        {
            "title": "John Ellis – Beta LLC",
            "url": "https://www.linkedin.com/in/john-ellis-beta",
            "description": "John Ellis, Founder at Beta LLC",
        },
        {
            "title": "John Fitzgerald – Gamma",
            "url": "https://www.linkedin.com/in/john-fitzgerald-gamma",
            "description": "John Fitzgerald, Engineer at Gamma",
        },
    ]

    def _fake(query: str, brave_key: str, serper_key: str) -> list[dict]:
        q = query.lower()
        if '"john@corp.com"' in q:
            return list(email_exact_results)
        if '"john"' in q and ("linkedin" in q or "researchgate" in q):
            return list(email_username_results)
        # Anything else — empty. We don't exercise those branches here.
        return []

    return _fake


# ── Tests ────────────────────────────────────────────────────────────


def test_email_exact_tagged_but_email_username_not() -> None:
    """Candidates from email-exact are flagged; email-username are not."""

    profile = Profile(name="John Smith", email="john@corp.com")
    resolver = IdentityResolver(brave_api_key="fake", serper_api_key="fake")

    with patch.object(identity, "_web_search", side_effect=_fake_web_search_factory()), \
         patch.object(identity, "_fetch_page", return_value=""):
        # Walk the actual resolver — but stub `_score_candidates` by
        # reading the candidate pool it assembles. Cleanest way is to
        # let resolve_profile run end-to-end, then introspect the log
        # and the returned ResolveResult. But _email_evidence is an
        # internal flag. So we monkey-patch _score_candidates to
        # capture the candidates list.
        captured = {}
        orig_score = resolver._score_candidates

        def capture_score(candidates, ctx, evc, log):
            captured["candidates"] = list(candidates)
            return orig_score(candidates, ctx, evc, log)

        resolver._score_candidates = capture_score  # type: ignore[method-assign]

        result = resolver.resolve_profile(profile)

    candidates = captured["candidates"]
    # Split by URL so the assertions are explicit.
    by_url = {c["url"]: c for c in candidates}

    # The email-exact LinkedIn must be tagged.
    exact_cand = by_url["https://www.linkedin.com/in/johnsmith-corp"]
    assert exact_cand.get("_email_evidence") is True, (
        f"email-exact candidate missing _email_evidence flag: {exact_cand}"
    )
    assert exact_cand.get("_email_evidence_type") == "exact", (
        f"email-exact candidate should be type=exact, got "
        f"{exact_cand.get('_email_evidence_type')!r}"
    )

    # Every email-username LinkedIn must NOT be tagged. This is the bug fix.
    username_urls = [
        "https://www.linkedin.com/in/john-baker-unrelated",
        "https://www.linkedin.com/in/johnchen-acme",
        "https://www.linkedin.com/in/john-davidson-alpha",
        "https://www.linkedin.com/in/john-ellis-beta",
        "https://www.linkedin.com/in/john-fitzgerald-gamma",
    ]
    for url in username_urls:
        assert url in by_url, f"missing expected username candidate {url}"
        c = by_url[url]
        assert not c.get("_email_evidence"), (
            f"email-username candidate {url} was flagged as email evidence — "
            f"this is the regressed bug. candidate={c}"
        )
        assert c.get("_email_evidence_type") is None, (
            f"email-username candidate {url} has evidence type "
            f"{c.get('_email_evidence_type')!r} — should be None"
        )

    # Resolver should settle on the real person (johnsmith-corp), because it
    # has +20 email-evidence + slug/name match, while the five Johns get no
    # email bonus. If this fails, scoring didn't pick up the evidence
    # difference.
    assert result.linkedin_url == "https://www.linkedin.com/in/johnsmith-corp", (
        f"resolver didn't pick the email-exact candidate; got {result.linkedin_url!r}\n"
        f"log:\n" + "\n".join(result.log)
    )


def test_no_pathological_score_23_tie_for_common_first_name() -> None:
    """Pre-fix, five unrelated `john-*` LinkedIns would tie at ~23
    (20 email-evidence + 2 first-in-title + 2 slug-name − 1 slug-no-last)
    and cause an ambiguous skip. After the fix, no such tie should occur.
    """

    profile = Profile(name="John Smith", email="john@corp.com")
    resolver = IdentityResolver(brave_api_key="fake", serper_api_key="fake")

    with patch.object(identity, "_web_search", side_effect=_fake_web_search_factory()), \
         patch.object(identity, "_fetch_page", return_value=""):
        result = resolver.resolve_profile(profile)

    # Parse the per-candidate score lines from the log. They look like:
    #   "  [ 23] https://...  (reasons)"
    scores = []
    for line in result.log:
        s = line.strip()
        if s.startswith("[") and "]" in s:
            try:
                score = int(s[1 : s.index("]")].strip())
                scores.append(score)
            except ValueError:
                continue

    # Count unrelated-John candidates sitting at exactly 23. Pre-fix this
    # would be ≥2 (the tell-tale bug signature). Post-fix it should be 0
    # (or at most 1 — the real person can be anywhere, but we're checking
    # for a TIE specifically).
    twenty_threes = sum(1 for s in scores if s == 23)
    assert twenty_threes <= 1, (
        f"pathological tie at score 23 detected ({twenty_threes} candidates) — "
        f"email-username LinkedIns are still getting the +20 evidence bonus.\n"
        f"all scores: {scores}\n"
        f"log:\n" + "\n".join(result.log)
    )

    # Also confirm the resolver didn't reject as ambiguous. After the fix,
    # there should be a clear winner.
    assert result.linkedin_url, (
        f"resolver rejected as ambiguous — fix regression.\n"
        f"error: {result.error}\n"
        f"log:\n" + "\n".join(result.log)
    )


def test_follow_email_evidence_runs_on_non_linkedin_pages() -> None:
    """Second bug: `_follow_email_evidence` was dead code because `search()`
    stripped non-LinkedIn results. After the fix, the non-LinkedIn page
    returned by email-exact (corp.com/team/john-smith) must be followed,
    and `_fetch_page` must be called for it (safe-domain: it's the
    profile's own email domain).
    """

    profile = Profile(name="John Smith", email="john@corp.com")
    resolver = IdentityResolver(brave_api_key="fake", serper_api_key="fake")

    fetch_calls: list[str] = []

    def fake_fetch(url, timeout=10):
        fetch_calls.append(url)
        # Return an HTML page that contains a LinkedIn URL
        return '<html><a href="https://www.linkedin.com/in/johnsmith-corp">profile</a></html>'

    with patch.object(identity, "_web_search", side_effect=_fake_web_search_factory()), \
         patch.object(identity, "_fetch_page", side_effect=fake_fetch):
        result = resolver.resolve_profile(profile)

    # We should have fetched the corp.com team page — the email appeared on
    # it and corp.com is the profile's own email domain (safe).
    assert any("corp.com/team/john-smith" in u for u in fetch_calls), (
        f"_follow_email_evidence never fetched the non-LinkedIn email-exact "
        f"page. fetch_calls={fetch_calls}\n"
        f"log:\n" + "\n".join(result.log)
    )


def test_follow_email_evidence_skips_broker_domains() -> None:
    """Cost-safety check: spokeo.com / beenverified.com / rocketreach.com
    style pages are NEVER fetched, even if they show up in a search
    result. Snippets can still be mined, but HTML fetch is gated.
    """

    profile = Profile(name="John Smith", email="john@corp.com")
    resolver = IdentityResolver(brave_api_key="fake", serper_api_key="fake")

    broker_only = [
        {
            "title": "John Smith | Spokeo",
            "url": "https://www.spokeo.com/john-smith",
            "description": "Contact info for John Smith.",
        },
        {
            "title": "John Smith | BeenVerified",
            "url": "https://www.beenverified.com/people/john-smith/",
            "description": "Background check for John Smith.",
        },
        {
            "title": "John Smith | RocketReach",
            "url": "https://www.rocketreach.co/john-smith",
            "description": "john@corp.com profile.",
        },
    ]

    def fake_search(query, brave_key, serper_key):
        q = query.lower()
        if '"john@corp.com"' in q:
            return list(broker_only)
        if '"john"' in q:
            return []
        return []

    fetch_calls: list[str] = []

    def fake_fetch(url, timeout=10):
        fetch_calls.append(url)
        return "<html></html>"

    with patch.object(identity, "_web_search", side_effect=fake_search), \
         patch.object(identity, "_fetch_page", side_effect=fake_fetch):
        resolver.resolve_profile(profile)

    for url in fetch_calls:
        ul = url.lower()
        assert "spokeo" not in ul, f"fetched broker domain: {url}"
        assert "beenverified" not in ul, f"fetched broker domain: {url}"
        assert "rocketreach" not in ul, f"fetched broker domain: {url}"


# ── Entrypoint ───────────────────────────────────────────────────────


def main() -> int:
    tests = [
        ("email_exact_tagged_but_email_username_not",
         test_email_exact_tagged_but_email_username_not),
        ("no_pathological_score_23_tie_for_common_first_name",
         test_no_pathological_score_23_tie_for_common_first_name),
        ("follow_email_evidence_runs_on_non_linkedin_pages",
         test_follow_email_evidence_runs_on_non_linkedin_pages),
        ("follow_email_evidence_skips_broker_domains",
         test_follow_email_evidence_skips_broker_domains),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}")
            print(f"  {e}")
        except Exception as e:  # pragma: no cover
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print(f"All {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
