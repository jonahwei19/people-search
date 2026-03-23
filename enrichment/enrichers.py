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
        self._cost_per_call = 0.10

    def enrich_profile(self, profile: Profile) -> EnrichmentResult:
        """Enrich a single profile's LinkedIn data, then verify the match."""
        url = profile.linkedin_url
        if not url or not is_valid_linkedin_url(url):
            return EnrichmentResult(success=False, error="No valid LinkedIn URL")

        url = normalize_linkedin_url(url)
        data = self._call_api(url)

        if data is None:
            return EnrichmentResult(success=False, error="API returned no data")
        if data == "OUT_OF_CREDITS":
            return EnrichmentResult(success=False, error="Out of API credits")

        parsed = self._parse_response(data)

        # ── Verify the enriched profile actually matches the person ──
        verified, verify_log = self._verify_match(profile, parsed)
        profile.enrichment_log.extend(verify_log)

        if not verified:
            # Wrong person — clear the LinkedIn URL, don't store wrong data
            profile.enrichment_log.append(f"REJECTED LinkedIn: {url} (wrong person)")
            profile.linkedin_url = ""
            profile.enrichment_status = EnrichmentStatus.FAILED
            return EnrichmentResult(success=False, error="LinkedIn profile doesn't match (verification failed)")

        profile.linkedin_enriched = parsed
        profile.enrichment_status = EnrichmentStatus.ENRICHED

        # Backfill identity fields from enrichment if missing
        if not profile.name and parsed.get("full_name"):
            profile.name = parsed["full_name"]
        if not profile.organization and parsed.get("current_company"):
            profile.organization = parsed["current_company"]
        if not profile.title and parsed.get("current_title"):
            profile.title = parsed["current_title"]

        return EnrichmentResult(
            success=True, data=parsed, cost=self._cost_per_call
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
        enriched_name = (enriched.get("full_name") or "").lower()
        profile_name = (profile.name or "").lower()
        if enriched_name and profile_name:
            profile_parts = set(profile_name.split())
            enriched_parts = set(enriched_name.split())
            # At least one name part must match (handles First Last vs. Last First)
            overlap = profile_parts & enriched_parts
            if len(overlap) >= 1:
                score += 2
                log.append(f"  Verify name: MATCH ({overlap})")
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

        if profile_org and any(all_companies):
            checks += 1
            org_words = [w for w in re.split(r'[\s&,]+', profile_org) if len(w) > 3]
            matched = False
            for company in all_companies:
                if not company:
                    continue
                # Full match or word overlap
                if profile_org in company or company in profile_org:
                    matched = True
                    break
                company_words = set(re.split(r'[\s&,]+', company))
                if org_words and any(w in company_words for w in org_words):
                    matched = True
                    break
            if matched:
                score += 3
                log.append(f"  Verify org: MATCH ('{profile_org}' found in experience)")
            else:
                score -= 2
                log.append(f"  Verify org: MISMATCH ('{profile_org}' not in {[c for c in all_companies if c][:3]})")

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
                score -= 2
                log.append(f"  Verify location: MISMATCH ('{profile_city or profile_country}' vs '{enriched_location}')")

        # Decision: name must match, and if we have org/location to check,
        # at least one MUST confirm. Org mismatch + location mismatch = definite reject.
        if checks == 0:
            log.append(f"  Verify result: ACCEPTED (name match, no org/location to check)")
            return True, log

        if score >= 3:
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

        # Mark profiles without LinkedIn URLs
        for p in profiles:
            if p.enrichment_status == EnrichmentStatus.PENDING:
                if not is_valid_linkedin_url(p.linkedin_url):
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
