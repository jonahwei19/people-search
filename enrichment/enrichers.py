"""LinkedIn enrichment and identity resolution.

Generalized from the TLS-specific enrich_linkedin.py. Handles:
- LinkedIn URL → full profile via EnrichLayer API
- Name + email/org → LinkedIn URL resolution (identity resolution)
- Rate limiting, retries, batch saves
"""

from __future__ import annotations

import os
import re
import time
import sys
from dataclasses import dataclass
from typing import Callable

import requests

from .models import Profile, EnrichmentStatus


ENRICHLAYER_ENDPOINT = "https://enrichlayer.com/api/v2/profile"


def is_valid_linkedin_url(url: str) -> bool:
    """Filter out junk LinkedIn URLs."""
    if not url:
        return False
    if "/in/" not in url and "/company/" not in url:
        return False
    bad = ["/public-profile/settings", "/feed/", "/pub/dir/"]
    return not any(b in url for b in bad)


def normalize_linkedin_url(url: str) -> str:
    """Clean up LinkedIn URL to canonical form."""
    url = url.strip().rstrip("/")
    # Strip query params and fragments
    url = re.sub(r"[?#].*$", "", url)
    # Ensure https
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url


@dataclass
class EnrichmentResult:
    """Result of enriching a single profile."""
    success: bool
    data: dict | None = None
    error: str | None = None
    cost: float = 0.0


class LinkedInEnricher:
    """Enrich profiles via EnrichLayer API."""

    def __init__(
        self,
        api_key: str | None = None,
        rate_limit: float = 0.1,
        batch_size: int = 20,
    ):
        self.api_key = api_key or os.environ.get("ENRICHLAYER_API_KEY", "")
        self.rate_limit = rate_limit
        self.batch_size = batch_size
        self._cost_per_call = 0.0168

    def enrich_profile(self, profile: Profile) -> EnrichmentResult:
        """Enrich a single profile's LinkedIn data, then verify the match.

        If verification fails, tries alternative LinkedIn URLs (stored during
        identity resolution) before giving up.
        """
        # Collect all candidate URLs: primary + alternatives
        urls_to_try = []
        if profile.linkedin_url and is_valid_linkedin_url(profile.linkedin_url):
            urls_to_try.append(profile.linkedin_url)
        # Add alternatives from enrichment log (stored by identity resolver)
        for line in profile.enrichment_log:
            if "linkedin.com/in/" in line:
                import re as _re
                for m in _re.finditer(r'https?://(?:\w+\.)?linkedin\.com/in/[\w-]+', line):
                    url = m.group(0)
                    if url not in urls_to_try:
                        urls_to_try.append(url)

        if not urls_to_try:
            return EnrichmentResult(success=False, error="No valid LinkedIn URL")

        total_cost = 0.0
        for url in urls_to_try:
            url = normalize_linkedin_url(url)
            profile.enrichment_log.append(f"Trying LinkedIn: {url}")

            data = self._call_api(url)
            if data == "OUT_OF_CREDITS":
                return EnrichmentResult(success=False, error="Out of API credits", cost=total_cost)
            if data is None:
                profile.enrichment_log.append(f"  → API returned no data")
                total_cost += self._cost_per_call
                continue

            total_cost += self._cost_per_call
            parsed = self._parse_response(data)

            # Verify the enriched profile matches the person.
            # `url` is passed so `_verify_match` can check the URL's slug as
            # an additional corroborating signal (P4): when the profile has a
            # confident name match AND the slug contains both first+last, we
            # bump the score; when the slug's last-name component matches the
            # ENRICHED name's last (rather than the profile's), we note it as
            # a weak counter-signal.
            verified, verify_log = self._verify_match(profile, parsed, url)
            profile.enrichment_log.extend(verify_log)

            if verified:
                profile.linkedin_url = url
                profile.linkedin_enriched = parsed
                profile.enrichment_status = EnrichmentStatus.ENRICHED

                # Backfill rules (FM5):
                # - name: fill only when missing (already-safe convention).
                # - organization / title: NEVER overwrite user-provided values.
                #   The user's upload is ground truth; LinkedIn is secondary
                #   evidence that can be wrong-person. Keep enrichment-sourced
                #   values in separate `enriched_*` fields so downstream code
                #   can decide how to weight them without contaminating the
                #   inputs to the next enrichment pass.
                if not profile.name and parsed.get("full_name"):
                    profile.name = parsed["full_name"]
                if parsed.get("current_company"):
                    profile.enriched_organization = parsed["current_company"]
                    if not profile.organization:
                        profile.organization = parsed["current_company"]
                if parsed.get("current_title"):
                    profile.enriched_title = parsed["current_title"]
                    if not profile.title:
                        profile.title = parsed["current_title"]

                return EnrichmentResult(success=True, data=parsed, cost=total_cost)
            else:
                profile.enrichment_log.append(f"  REJECTED: {url} (wrong person)")

        # All candidates failed verification
        profile.linkedin_url = ""
        profile.enrichment_status = EnrichmentStatus.FAILED
        return EnrichmentResult(
            success=False, error=f"All {len(urls_to_try)} LinkedIn candidates failed verification",
            cost=total_cost,
        )

    def _verify_match(
        self,
        profile: Profile,
        enriched: dict,
        linkedin_url: str = "",
    ) -> tuple[bool, list[str]]:
        """Cross-check enriched LinkedIn data against what we know about the person.

        Compares name, company, and location. Returns (is_match, log_lines).

        Scoring model (FM2 / P2 fix):
        - Name match is scored in three tiers:
            strong (+3): both first AND last name overlap, each ≥5 chars of
                         unique content (uncommon full-name match).
            normal (+2): both first and last overlap but shorter, OR strong
                         prefix/exact match with full name.
            weak   (+1): single-token overlap, or short common names; cannot
                         be accepted alone — requires another positive signal.
        - Positive non-name signals tracked: org match, location match,
          content-relevance match.
        - Soft penalties tracked: org mismatch, location mismatch, content
          relevance WEAK.
        - Acceptance rule: score >= 2 AND (no soft penalty OR at least one
          positive non-name signal). This closes the "name match + org
          mismatch, no other evidence" false-positive path that let
          same-name-different-person slip through at score=2, checks=1.

        P4 (slug-aware verification):
        - `linkedin_url` is parsed to extract the URL slug (the path segment
          after /in/). The slug is tokenised and substring-checked against
          the profile's first/last name.
        - If BOTH first+last are present in the slug, award +2 as a positive
          non-name signal (the URL itself corroborates the name-match).
        - If only the first name is in the slug: 0 (neutral).
        - If neither is in the slug: 0.
        - If the slug's last-name component matches the ENRICHED person's
          last name (not the profile's), log it as a soft counter-signal
          but do NOT subtract — the slug is the enriched profile's slug,
          so of course it contains the enriched name; we just flag that the
          slug does NOT corroborate the profile.
        """
        log = []
        score = 0
        checks = 0
        positive_non_name = 0
        soft_penalties = 0
        name_strength = "none"  # "none" / "weak" / "normal" / "strong"
        # Track verification anchors for structured observability (P5). Populated
        # alongside the human-readable log lines so the eval harness can slice
        # failures by reason category without re-parsing strings.
        anchors_positive: list[str] = []
        anchors_negative: list[str] = []

        # Name check (required)
        import unicodedata
        def _normalize(s):
            """Strip diacriticals: Zoltán → Zoltan, Šimon → Simon"""
            return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()

        enriched_name = _normalize(enriched.get("full_name") or "")
        profile_name = _normalize(profile.name or "")
        # Strip suffixes like ", Ph.D.", ", MPA" and abbreviation dots
        enriched_name = re.sub(r',?\s*(ph\.?d\.?|mpa|mba|m\.?d\.?|jr\.?|sr\.?|iii?|iv)$', '', enriched_name, flags=re.IGNORECASE).strip()
        enriched_name = enriched_name.replace(".", "")

        if enriched_name and profile_name:
            profile_parts_list = profile_name.replace("-", " ").split()
            enriched_parts_list = enriched_name.replace("-", " ").split()
            profile_parts = set(profile_parts_list)
            enriched_parts = set(enriched_parts_list)
            # Direct overlap
            overlap = profile_parts & enriched_parts
            # Also check prefix matching (Jen→Jennifer, Alex→Alexander).
            # Requires ≥4 chars on both sides to avoid e.g. "Li" matching "Lisa".
            if len(overlap) < 1:
                for pp in profile_parts:
                    for ep in enriched_parts:
                        if len(pp) >= 4 and len(ep) >= 4 and (pp.startswith(ep) or ep.startswith(pp)):
                            overlap.add(pp)

            # Spurious-match guard: if the profile has both first+last name and
            # only ONE short token matches, reject. "Abi" (3 chars) matching
            # "abi-hashem" while "olvera" is missing is a false match — too many
            # common short name tokens (Abi, Li, Sam, Kim, An, etc.) collide
            # across totally different people.
            profile_has_first_last = len(profile_parts_list) >= 2
            profile_first = profile_parts_list[0] if profile_parts_list else ""
            profile_last = profile_parts_list[-1] if len(profile_parts_list) >= 2 else ""

            if overlap:
                single_short_match = (
                    profile_has_first_last
                    and len(overlap) == 1
                    and max(len(p) for p in overlap) < 5
                )
                last_name_missing = (
                    profile_has_first_last
                    and profile_last
                    and profile_last not in overlap
                    # Last name missing from enriched entirely (not just from overlap)
                    and profile_last not in enriched_name
                )
                if single_short_match or last_name_missing:
                    reason_code = "short-token-only" if single_short_match else "last-name-missing"
                    log.append(
                        f"  Verify name: WEAK-MATCH REJECTED "
                        f"(overlap={overlap}, profile='{profile_name}', enriched='{enriched_name}', "
                        f"reason={reason_code})"
                    )
                    anchors_negative.append(f"weak_match_{reason_code.replace('-', '_')}")
                    self._record_verification_decision(
                        profile,
                        linkedin_url=linkedin_url,
                        enriched_name=enriched_name,
                        score=score,
                        anchors_positive=anchors_positive,
                        anchors_negative=anchors_negative,
                        decision="reject",
                        reason=f"weak name match rejected ({reason_code})",
                    )
                    return False, log

            if len(overlap) >= 1:
                # Strength classification (FM2 / P2):
                # - strong: both first+last overlap, each ≥5 chars of unique
                #           content ("Renee", "DiResta"). Uncommon enough that
                #           a chance collision with a different person is rare.
                # - normal: both first+last overlap but shorter (or one side
                #           is a 4-char prefix / known short name).
                # - weak:   single-token overlap on a first-name-only profile,
                #           OR a short (<5 char) token pair. Needs corroboration.
                overlap_lens = sorted(len(p) for p in overlap)
                both_present = (
                    profile_has_first_last
                    and profile_first in overlap
                    and profile_last in overlap
                )
                if both_present and len(profile_first) >= 5 and len(profile_last) >= 5:
                    score += 3
                    name_strength = "strong"
                    anchors_positive.append("name_strong")
                elif both_present:
                    score += 2
                    name_strength = "normal"
                    anchors_positive.append("name_normal")
                elif len(overlap) >= 2 and overlap_lens[0] >= 4:
                    # Two tokens overlap but profile didn't cleanly give us
                    # first+last (e.g., hyphenated or multi-token names).
                    score += 2
                    name_strength = "normal"
                    anchors_positive.append("name_normal")
                else:
                    # Single-token overlap. Profile may be a one-word name
                    # ("Beez Africa") or a first-name collision. Weak — needs
                    # another positive signal before we'll accept.
                    score += 1
                    name_strength = "weak"
                    anchors_positive.append("name_weak")
                log.append(f"  Verify name: MATCH ({overlap}, strength={name_strength})")
            else:
                log.append(f"  Verify name: MISMATCH ('{profile_name}' vs '{enriched_name}')")
                anchors_negative.append("name_mismatch")
                self._record_verification_decision(
                    profile,
                    linkedin_url=linkedin_url,
                    enriched_name=enriched_name,
                    score=score,
                    anchors_positive=anchors_positive,
                    anchors_negative=anchors_negative,
                    decision="reject",
                    reason=f"name mismatch ('{profile_name}' vs '{enriched_name}')",
                )
                return False, log  # Name mismatch is a hard reject

        # P4: Slug-aware corroboration.
        # Parse the LinkedIn URL's path slug (e.g., linkedin.com/in/dan-fragiadakis-phd
        # → "dan-fragiadakis-phd"), then check whether the profile's first and
        # last name appear as substrings inside it. A slug that contains BOTH
        # is a URL-level corroboration of the name match (LinkedIn constructs
        # /in/ slugs from the profile owner's name at signup, so "dylan-matthews"
        # in the slug is strong evidence the account belongs to Dylan Matthews).
        # Worth +2 as a positive non-name signal (the URL is an *independent*
        # anchor from the display-name match).
        #
        # Neutral cases (no score change): first name only in slug, neither in
        # slug. LinkedIn sometimes auto-generates numeric/abbreviated slugs
        # (/in/abc-12345), so "slug doesn't contain profile name" is not by
        # itself evidence against the match — just an absence of corroboration.
        #
        # Counter-signal (logged, no score change): if the slug's last-name
        # component matches the ENRICHED last name but NOT the profile's last
        # name. Example: profile="Abi Olvera", enriched="Abi Hashem", slug=
        # "abi-hashem" — same first name, slug corroborates the *enriched*
        # person, not the profile. We don't subtract (the name check already
        # handles last-name-missing as a hard reject upstream), but we note it
        # so the arbiter / eval harness can see the pattern.
        if linkedin_url:
            slug = linkedin_url.rstrip("/").split("/")[-1].lower()
            slug_clean = slug.replace("-", "").replace("_", "").replace(".", "")
            if slug_clean:
                pf = profile_first if profile_first else ""
                pl = profile_last if profile_last else ""
                first_in_slug = bool(pf) and len(pf) >= 3 and pf in slug_clean
                last_in_slug = bool(pl) and len(pl) >= 3 and pl in slug_clean

                if first_in_slug and last_in_slug:
                    score += 2
                    positive_non_name += 1
                    anchors_positive.append("slug_match")
                    log.append(f"  Verify slug: MATCH ('{slug}' contains both '{pf}' and '{pl}')")
                elif first_in_slug and not last_in_slug:
                    # First-only is neutral — legitimate abbreviated or
                    # privacy-hidden-last-name cases land here.
                    log.append(f"  Verify slug: PARTIAL ('{slug}' contains '{pf}' but not '{pl}')")
                else:
                    # Neither name in slug — could be a numeric slug, or the
                    # slug belongs to a different person entirely. Check whether
                    # the slug corroborates the *enriched* last name instead —
                    # that's a weak counter-signal we want visibility on.
                    enriched_last = ""
                    if enriched_name:
                        e_parts = enriched_name.replace("-", " ").split()
                        if len(e_parts) >= 2:
                            enriched_last = e_parts[-1]
                    if enriched_last and len(enriched_last) >= 3 and enriched_last in slug_clean and enriched_last != pl:
                        # Note, don't subtract. The slug being "hashem" when
                        # the profile is "Abi Olvera" is informative: the slug
                        # belongs to the enriched (possibly wrong) person.
                        anchors_negative.append("slug_matches_enriched_not_profile")
                        log.append(
                            f"  Verify slug: COUNTER-SIGNAL "
                            f"('{slug}' corroborates enriched last '{enriched_last}', not profile last '{pl}')"
                        )
                    else:
                        log.append(f"  Verify slug: NONE ('{slug}' contains neither '{pf}' nor '{pl}')")

        # Company/org check
        profile_org = (profile.organization or "").lower()
        enriched_company = (enriched.get("current_company") or "").lower()
        # Also check full experience history
        all_companies = [enriched_company]
        for exp in enriched.get("experience", []):
            all_companies.append((exp.get("company") or "").lower())

        # Skip org check for vague/uninformative org names
        vague_orgs = {"n/a", "self-employed", "self employed", "freelance", "independent",
                      "stealth", "stealth startup", "none", "na", "personal", ""}
        # Extract just the company name (after "at" if present)
        org_for_check = profile_org.split(" at ")[-1].strip() if " at " in profile_org else profile_org
        vague_starters = ("stealth", "new ", "my ", "own ", "various", "startup")
        org_is_vague = (org_for_check in vague_orgs
                        or any(org_for_check.startswith(v) for v in vague_starters)
                        or org_for_check in ("", "n/a"))

        if profile_org and any(all_companies) and not org_is_vague:
            checks += 1
            # Use just the company name (after "at" if present), not the title
            org_to_match = org_for_check
            org_words = [w for w in re.split(r'[\s&,]+', org_to_match) if len(w) > 3]
            matched = False
            for company in all_companies:
                if not company:
                    continue
                # Full match or word overlap
                if org_to_match in company or company in org_to_match:
                    matched = True
                    break
                company_words = set(re.split(r'[\s&,]+', company))
                if org_words and any(w in company_words for w in org_words):
                    matched = True
                    break
            if matched:
                score += 3
                positive_non_name += 1
                anchors_positive.append("org_match")
                log.append(f"  Verify org: MATCH ('{org_to_match}' found in experience)")
            else:
                # Soft penalty — people change jobs, orgs get renamed
                score -= 1
                soft_penalties += 1
                anchors_negative.append("org_mismatch")
                log.append(f"  Verify org: MISMATCH ('{org_to_match}' not in {[c for c in all_companies if c][:3]})")
        elif org_is_vague:
            log.append(f"  Verify org: SKIPPED (vague org: '{profile_org}')")

        # Location check
        profile_country = ""
        profile_city = ""
        for key, val in profile.metadata.items():
            if not val:
                continue
            key_lower = key.lower()
            if "country" in key_lower or "region" in key_lower:
                profile_country = str(val).lower()
            if "city" in key_lower or "nearest" in key_lower:
                profile_city = str(val).lower()

        enriched_location = (enriched.get("location") or "").lower()
        if (profile_country or profile_city) and enriched_location:
            checks += 1
            loc_match = False
            if profile_city and profile_city in enriched_location:
                loc_match = True
            if profile_country and not loc_match:
                # Use the FIRST word of the country (the actual country name),
                # not generic words like "united", "republic", "of"
                country_name = profile_country.split(",")[0].strip()  # "Tanzania" from "Tanzania, United Republic of"
                if len(country_name) > 3 and country_name in enriched_location:
                    loc_match = True
            if loc_match:
                score += 2
                positive_non_name += 1
                anchors_positive.append("location_match")
                log.append(f"  Verify location: MATCH ('{enriched_location}')")
            else:
                # Soft penalty — people relocate
                score -= 1
                soft_penalties += 1
                anchors_negative.append("location_mismatch")
                log.append(f"  Verify location: MISMATCH ('{profile_city or profile_country}' vs '{enriched_location}')")

        # Thin profile note: many early-career people have sparse LinkedIn.
        # Don't penalize — name match is the primary signal. Just log it.
        exp_count = len(enriched.get("experience", []))
        has_headline = bool(enriched.get("headline", "").strip() and enriched.get("headline") != "--")
        if exp_count == 0 and not has_headline:
            log.append(f"  Verify thin profile: NOTE (0 experiences, no headline — common for early-career)")

        # Content-relevance check: if the profile has substantial content
        # (pitch, bio, notes), check if the enriched LinkedIn has ANY overlap
        # with key terms from that content. A boilermaker LinkedIn for a
        # biosecurity pitcher is a red flag.
        content_text = " ".join(v for v in profile.content_fields.values() if v).lower()
        if len(content_text) > 200:
            enriched_text = (enriched.get("context_block") or "").lower()
            if enriched_text:
                checks += 1
                # Extract key terms from content (5+ char words, not stopwords)
                import re as _re
                content_words = set(_re.findall(r'\b[a-z]{5,}\b', content_text))
                enriched_words = set(_re.findall(r'\b[a-z]{5,}\b', enriched_text))
                stop = {"about", "their", "these", "those", "would", "could", "should",
                        "being", "other", "which", "through", "between", "before", "after"}
                content_words -= stop
                enriched_words -= stop
                content_overlap = content_words & enriched_words
                if len(content_overlap) >= 3:
                    score += 2
                    positive_non_name += 1
                    anchors_positive.append("content_match")
                    log.append(f"  Verify content relevance: MATCH ({len(content_overlap)} shared terms: {list(content_overlap)[:5]})")
                elif len(content_overlap) == 0 and len(content_words) > 10:
                    score -= 1
                    soft_penalties += 1
                    anchors_negative.append("content_mismatch")
                    log.append(f"  Verify content relevance: WEAK (zero overlap between content and LinkedIn)")

        # Decision rules (FM2 / P2):
        # 1. Baseline threshold: score >= 2.
        # 2. If ANY soft penalty fired, require at least one positive non-name
        #    signal. Name-match-alone at exactly threshold is no longer enough
        #    — a lone org mismatch or location mismatch would flip acceptance,
        #    and in practice those ARE wrong-person cases.
        # 3. Weak name match (single token / short common names) is never
        #    accepted without a corroborating positive non-name signal.
        if score < 2:
            log.append(f"  Verify result: REJECTED (score={score}, checks={checks} — likely wrong person)")
            self._record_verification_decision(
                profile,
                linkedin_url=linkedin_url,
                enriched_name=enriched_name,
                score=score,
                anchors_positive=anchors_positive,
                anchors_negative=anchors_negative,
                decision="reject",
                reason=f"score_below_threshold (score={score}, checks={checks})",
            )
            return False, log

        if name_strength == "weak" and positive_non_name == 0:
            log.append(
                f"  Verify result: REJECTED (weak name match with no corroborating signal; "
                f"score={score}, checks={checks}, penalties={soft_penalties})"
            )
            self._record_verification_decision(
                profile,
                linkedin_url=linkedin_url,
                enriched_name=enriched_name,
                score=score,
                anchors_positive=anchors_positive,
                anchors_negative=anchors_negative,
                decision="reject",
                reason=f"weak_name_no_corroboration (score={score}, penalties={soft_penalties})",
            )
            return False, log

        if soft_penalties > 0 and positive_non_name == 0:
            log.append(
                f"  Verify result: REJECTED (soft penalty without corroborating positive; "
                f"score={score}, checks={checks}, penalties={soft_penalties})"
            )
            self._record_verification_decision(
                profile,
                linkedin_url=linkedin_url,
                enriched_name=enriched_name,
                score=score,
                anchors_positive=anchors_positive,
                anchors_negative=anchors_negative,
                decision="reject",
                reason=f"soft_penalty_no_positive (score={score}, penalties={soft_penalties})",
            )
            return False, log

        log.append(
            f"  Verify result: ACCEPTED (score={score}, checks={checks}, "
            f"name={name_strength}, positives={positive_non_name}, penalties={soft_penalties})"
        )
        self._record_verification_decision(
            profile,
            linkedin_url=linkedin_url,
            enriched_name=enriched_name,
            score=score,
            anchors_positive=anchors_positive,
            anchors_negative=anchors_negative,
            decision="accept",
            reason=(
                f"accepted (name={name_strength}, positives={positive_non_name}, "
                f"penalties={soft_penalties})"
            ),
        )
        return True, log

    @staticmethod
    def _record_verification_decision(
        profile: Profile,
        *,
        linkedin_url: str,
        enriched_name: str,
        score: int,
        anchors_positive: list[str],
        anchors_negative: list[str],
        decision: str,
        reason: str,
    ) -> None:
        """Append a structured observability record for one verification pass.

        Each entry captures the attempted match, the anchors that fired, and
        the final decision — so the eval harness can slice failures by
        reason without re-parsing the free-form enrichment_log.

        Defensive: profiles loaded from older DB rows may not have the
        `verification_decisions` attribute yet. Treat a missing attribute
        as an empty list and initialise it in place.
        """
        from datetime import datetime, timezone

        decisions = getattr(profile, "verification_decisions", None)
        if decisions is None:
            decisions = []
            try:
                profile.verification_decisions = decisions
            except Exception:
                # Profile may be a frozen/slotted variant — bail out silently.
                return
        decisions.append(
            {
                "linkedin_url": linkedin_url or "",
                "enriched_name": enriched_name or "",
                "score": int(score),
                "anchors_positive": list(anchors_positive),
                "anchors_negative": list(anchors_negative),
                "decision": decision,
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def enrich_batch(
        self,
        profiles: list[Profile],
        on_progress: Callable[[int, int, str], None] | None = None,
        on_batch_save: Callable[[], None] | None = None,
        max_workers: int = 10,
    ) -> dict:
        """Enrich a batch of profiles in parallel.

        Args:
            profiles: Profiles to enrich (modifies in place)
            on_progress: Callback(current, total, message)
            on_batch_save: Called periodically for incremental saves
            max_workers: Number of parallel enrichment threads

        Returns:
            Stats dict: {enriched, skipped, failed, total_cost}
        """
        import concurrent.futures

        enrichable = [
            p for p in profiles
            if p.enrichment_status == EnrichmentStatus.PENDING
            and is_valid_linkedin_url(p.linkedin_url)
        ]

        stats = {"enriched": 0, "skipped": 0, "failed": 0, "total_cost": 0.0}
        completed = [0]
        _credits_dead = [False]

        def do_enrich(profile: Profile) -> tuple[Profile, EnrichmentResult]:
            if _credits_dead[0]:
                return profile, EnrichmentResult(success=False, error="Out of API credits")
            result = self.enrich_profile(profile)
            if result.error == "Out of API credits":
                _credits_dead[0] = True
            return profile, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(do_enrich, p): p for p in enrichable}

            for future in concurrent.futures.as_completed(futures):
                profile, result = future.result()
                completed[0] += 1

                if on_progress:
                    on_progress(completed[0], len(enrichable), f"Enriched {profile.display_name()}")

                if result.success:
                    stats["enriched"] += 1
                    stats["total_cost"] += result.cost
                    exp_count = len(profile.linkedin_enriched.get("experience", []))
                    profile.enrichment_log.append(f"LinkedIn enriched: {exp_count} experiences, {profile.linkedin_enriched.get('headline', '')}")
                else:
                    stats["failed"] += 1
                    profile.enrichment_status = EnrichmentStatus.FAILED
                    profile.enrichment_log.append(f"LinkedIn enrichment failed: {result.error}")

                if completed[0] % self.batch_size == 0 and on_batch_save:
                    on_batch_save()

        # Handle profiles without valid LinkedIn URLs — their linkedin_url
        # field might contain a website, Google Drive link, etc. Fetch that
        # content and move the URL to the right field.
        from .fetchers import fetch_link

        no_linkedin = [
            p for p in profiles
            if p.enrichment_status == EnrichmentStatus.PENDING
            and not is_valid_linkedin_url(p.linkedin_url)
        ]

        for p in no_linkedin:
            url = (p.linkedin_url or "").strip()
            if url and url.startswith("http"):
                # Fetch content from whatever URL they put in the LinkedIn field
                result = fetch_link(url)
                if result.success:
                    p.fetched_content[result.source] = result.text
                    p.enrichment_log.append(
                        f"Non-LinkedIn URL fetched as {result.source}: {url[:60]}"
                    )
                # Move URL to the right field so identity resolution can
                # still try to find their real LinkedIn
                url_lower = url.lower()
                if "drive.google.com" in url_lower or "docs.google.com" in url_lower:
                    p.resume_url = p.resume_url or url
                elif "github.com" in url_lower:
                    if not p.other_links or url not in p.other_links:
                        p.other_links.append(url)
                else:
                    p.website_url = p.website_url or url
                p.linkedin_url = ""

            # Mark as enriched (we got what we could) rather than skipped
            if p.fetched_content:
                p.enrichment_status = EnrichmentStatus.ENRICHED
                stats["enriched"] += 1
                p.enrichment_log.append("Enriched from non-LinkedIn URL content")
            else:
                p.enrichment_status = EnrichmentStatus.SKIPPED
                stats["skipped"] += 1

        return stats

    def _call_api(self, linkedin_url: str, retries: int = 2) -> dict | str | None:
        """Call EnrichLayer API. Returns parsed JSON, 'OUT_OF_CREDITS', or None.

        Retries transient network failures (timeout / connection reset / 5xx)
        with exponential backoff. 429 rate-limit still uses Retry-After.
        """
        if not self.api_key:
            return None

        from ._retry import retry_request

        resp = retry_request(
            lambda: requests.get(
                ENRICHLAYER_ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}"},
                params={"profile_url": linkedin_url},
                timeout=30,
            ),
            max_attempts=3,
            base_delay=2.0,
            label=f"EnrichLayer {linkedin_url[-32:]}",
        )
        if resp is None:
            return None

        if resp.status_code in (402, 403) and "credits" in resp.text.lower():
            return "OUT_OF_CREDITS"

        if resp.status_code == 429:
            if retries > 0:
                retry_after = int(resp.headers.get("retry-after", 30))
                wait = min(retry_after + 5, 180)
                print(
                    f"  Rate limited, waiting {wait}s ({retries} retries left)...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                return self._call_api(linkedin_url, retries=retries - 1)
            return None

        if resp.status_code == 404:
            return None

        if resp.status_code != 200:
            print(
                f"  API error {resp.status_code}: {resp.text[:100]}",
                file=sys.stderr,
            )
            return None

        try:
            data = resp.json()
        except Exception as e:
            print(f"  JSON parse failed: {e}", file=sys.stderr)
            return None

        if isinstance(data, dict) and data.get("error"):
            return None

        return data

    def _parse_response(self, data: dict) -> dict:
        """Parse EnrichLayer response into our normalized format."""
        experience = []
        for e in data.get("experiences") or []:
            if not isinstance(e, dict):
                continue
            starts = e.get("starts_at") or {}
            ends = e.get("ends_at") or {}
            start_yr = str(starts.get("year", "")) if starts else ""
            end_yr = str(ends.get("year", "")) if ends else ""
            experience.append({
                "company": e.get("company", "") or "",
                "title": e.get("title", "") or "",
                "years": f"{start_yr}–{end_yr}" if start_yr else "",
                "description": e.get("description", "") or "",
            })

        education = []
        for e in data.get("education") or []:
            if not isinstance(e, dict):
                continue
            starts = e.get("starts_at") or {}
            ends = e.get("ends_at") or {}
            start_yr = str(starts.get("year", "")) if starts else ""
            end_yr = str(ends.get("year", "")) if ends else ""
            education.append({
                "school": e.get("school", "") or "",
                "degree": e.get("degree_name", "") or "",
                "field_of_study": e.get("field_of_study", "") or "",
                "years": f"{start_yr}–{end_yr}" if start_yr or end_yr else "",
            })

        current_title = experience[0].get("title", "") if experience else ""
        current_company = experience[0].get("company", "") if experience else ""

        full_name = data.get("full_name", "") or ""
        headline = data.get("headline", "") or data.get("occupation", "") or ""
        location = data.get("location_str", "") or data.get("city", "") or ""
        summary = data.get("summary", "") or ""

        # Build a searchable context block
        context_parts = [f"{full_name}"]
        if headline:
            context_parts.append(f"Headline: {headline}")
        if summary:
            context_parts.append(f"About: {summary}")
        if experience:
            context_parts.append("Experience:")
            for exp in experience[:10]:
                line = f"  {exp['title']} at {exp['company']}"
                if exp['years']:
                    line += f" ({exp['years']})"
                context_parts.append(line)
        if education:
            context_parts.append("Education:")
            for edu in education:
                line = f"  {edu['degree']} {edu['field_of_study']} — {edu['school']}"
                if edu['years']:
                    line += f" ({edu['years']})"
                context_parts.append(line)

        return {
            "full_name": full_name,
            "headline": headline,
            "current_company": current_company,
            "current_title": current_title,
            "location": location,
            "summary": summary,
            "experience": experience,
            "education": education,
            "context_block": "\n".join(context_parts),
        }
