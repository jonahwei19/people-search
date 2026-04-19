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

            # Verify the enriched profile matches the person
            verified, verify_log = self._verify_match(profile, parsed)
            profile.enrichment_log.extend(verify_log)

            if verified:
                profile.linkedin_url = url
                profile.linkedin_enriched = parsed
                profile.enrichment_status = EnrichmentStatus.ENRICHED

                # Backfill identity fields from enrichment if missing
                if not profile.name and parsed.get("full_name"):
                    profile.name = parsed["full_name"]
                if not profile.organization and parsed.get("current_company"):
                    profile.organization = parsed["current_company"]
                if not profile.title and parsed.get("current_title"):
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

    def _verify_match(self, profile: Profile, enriched: dict) -> tuple[bool, list[str]]:
        """Cross-check enriched LinkedIn data against what we know about the person.

        Compares name, company, and location. Returns (is_match, log_lines).
        A match requires the name to be plausible AND at least one of
        org/location to confirm (or no org/location to check against).
        """
        log = []
        score = 0
        checks = 0

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
                    log.append(
                        f"  Verify name: WEAK-MATCH REJECTED "
                        f"(overlap={overlap}, profile='{profile_name}', enriched='{enriched_name}', "
                        f"reason={'short-token-only' if single_short_match else 'last-name-missing'})"
                    )
                    return False, log

            if len(overlap) >= 1:
                # Unique/uncommon names are stronger signals
                name_len = sum(len(p) for p in overlap)
                if len(overlap) >= 2 and name_len >= 10:
                    score += 3  # Strong: both first+last match AND uncommon
                else:
                    score += 2
                log.append(f"  Verify name: MATCH ({overlap}, strength={'strong' if score >= 3 else 'normal'})")
            else:
                log.append(f"  Verify name: MISMATCH ('{profile_name}' vs '{enriched_name}')")
                return False, log  # Name mismatch is a hard reject

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
                log.append(f"  Verify org: MATCH ('{org_to_match}' found in experience)")
            else:
                # Soft penalty — people change jobs, orgs get renamed
                score -= 1
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
                log.append(f"  Verify location: MATCH ('{enriched_location}')")
            else:
                # Soft penalty — people relocate
                score -= 1
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
                overlap = content_words & enriched_words
                if len(overlap) >= 3:
                    score += 2
                    log.append(f"  Verify content relevance: MATCH ({len(overlap)} shared terms: {list(overlap)[:5]})")
                elif len(overlap) == 0 and len(content_words) > 10:
                    score -= 1
                    log.append(f"  Verify content relevance: WEAK (zero overlap between content and LinkedIn)")

        # Decision: name match (score >= 2) is the baseline. Additional checks
        # add or subtract. Accept if name matched and net score is positive.
        # With checks: name(+2) + org_mismatch(-1) + content_match(+2) = 3 → accept
        # Without checks: name(+2) alone is enough
        if score >= 2:
            log.append(f"  Verify result: ACCEPTED (score={score}, checks={checks})")
            return True, log
        else:
            log.append(f"  Verify result: REJECTED (score={score}, checks={checks} — likely wrong person)")
            return False, log

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
        """Call EnrichLayer API. Returns parsed JSON, 'OUT_OF_CREDITS', or None."""
        if not self.api_key:
            return None

        try:
            resp = requests.get(
                ENRICHLAYER_ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}"},
                params={"profile_url": linkedin_url},
                timeout=30,
            )

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

            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                return None

            return data

        except Exception as e:
            print(f"  Request failed: {e}", file=sys.stderr)
            return None

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
