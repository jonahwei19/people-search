"""Stage 4: LinkedIn resolve — thin wrapper over v1 IdentityResolver.

We reuse the v1 identity resolver largely as-is but:
    * Treat its returned LinkedIn URL as one Evidence (with platform_match
      + name_match anchors if v1 produced a medium/high-confidence hit).
    * Boost confidence if an org_site hit already exists for the same
      LinkedIn URL (that's two independent anchors: org_site + resolver).
    * Harvest the non-LinkedIn evidence URLs the resolver collects and
      hand them to the verifier as additional candidates.

Then Stage 4b calls EnrichLayer to actually fetch the LinkedIn profile
data, and v1's _verify_match is used to accept or reject.

This keeps the battle-tested path intact while letting Variant A+ skip
LinkedIn entirely when earlier stages already produced ≥2 strong anchors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enrichers import LinkedInEnricher, is_valid_linkedin_url, normalize_linkedin_url
from ..identity import IdentityResolver
from ..models import EnrichmentStatus, Profile
from .cohort import CohortSignals, slug_matches_url
from .evidence import Evidence


@dataclass
class LinkedInResult:
    linkedin_hits: list[Evidence]    # LinkedIn URL candidates (anchors attached)
    aux_hits: list[Evidence]         # Non-LinkedIn URLs the resolver discovered
    log: list[str]
    resolver_confidence: str = ""
    resolver_error: str = ""
    enriched_data: Optional[dict] = None   # populated if EnrichLayer succeeded


def resolve_linkedin(
    profile: Profile,
    signals: CohortSignals,
    resolver: IdentityResolver,
    enricher: Optional[LinkedInEnricher] = None,
    priors: Optional[list[Evidence]] = None,
    do_enrich: bool = True,
) -> LinkedInResult:
    """Run v1 IdentityResolver → if a URL is found, optionally enrich it.

    Args:
        profile: the profile being enriched (linkedin_url may already be set)
        signals: Stage-1 signals for anchor computation
        resolver: v1 IdentityResolver instance (shared across the batch)
        enricher: v1 LinkedInEnricher (if None, don't attempt EnrichLayer)
        priors: Evidence already gathered (used for anchor-boost matching)
        do_enrich: if False, only find LinkedIn URL candidates — skip API call

    Returns:
        LinkedInResult with evidence lists + any enriched data.
    """
    priors = priors or []
    log: list[str] = []

    # If the profile already had a LinkedIn URL on upload and it's valid,
    # skip identity resolution — go straight to enrichment.
    existing_url = profile.linkedin_url if is_valid_linkedin_url(profile.linkedin_url) else ""
    resolved_url = existing_url
    confidence = "high" if existing_url else ""
    resolver_error = ""

    if not existing_url:
        result = resolver.resolve_profile(profile)
        log.extend(result.log)
        if result.linkedin_url:
            resolved_url = result.linkedin_url
            confidence = result.confidence
        else:
            resolver_error = result.error or "no match"

    linkedin_hits: list[Evidence] = []
    aux_hits: list[Evidence] = []

    if resolved_url:
        url_norm = normalize_linkedin_url(resolved_url)
        anchors: set[str] = set()
        # slug_match: LinkedIn /in/<slug> contains name tokens
        slug_hit = slug_matches_url(signals.name_slugs, url_norm)
        if slug_hit:
            anchors.add("slug_match")
        # name_match: resolver accepted on name — approximate as name_match
        # only when resolver returned a medium/high-confidence verdict.
        if confidence in ("high", "medium"):
            anchors.add("name_match")
        anchors.add("platform_match")

        # Priors boost: if Stage 2 already produced an org_site hit whose
        # URL matches this LinkedIn URL, accumulate its anchors too.
        for e in priors:
            if e.source == "org_site" and _url_match(e.url, url_norm):
                anchors |= e.anchors

        linkedin_hits.append(Evidence(
            url=url_norm,
            source="linkedin",
            kind="linkedin",
            anchors=anchors,
            snippet="",  # filled in by enrichment below
            title="",
            raw={"confidence": confidence},
        ))

    # Also harvest evidence URLs from the resolver's non-LinkedIn fallback
    # search (they carry their own anchor hints via URL slug / org domain).
    try:
        resolver_evidence = getattr(result, "evidence_urls", []) if not existing_url else []  # type: ignore
    except Exception:
        resolver_evidence = []

    for e in resolver_evidence or []:
        url = (e.get("url") or "").strip()
        if not url:
            continue
        anchors: set[str] = set()
        url_lower = url.lower()
        if signals.org_domain and signals.org_domain in url_lower:
            anchors.add("email_match")
        slug_hit = slug_matches_url(signals.name_slugs, url_lower)
        if slug_hit:
            anchors.add("slug_match")
        # snippet-based name check
        desc = (e.get("description") or "").lower()
        title = (e.get("title") or "").lower()
        combined = f"{title} {desc}"
        if signals.first and signals.last:
            if signals.first in combined and signals.last in combined:
                anchors.add("name_match")
        if anchors:
            aux_hits.append(Evidence(
                url=url,
                source="open_web",
                kind="website",
                anchors=anchors,
                snippet=(e.get("description") or "")[:400],
                title=e.get("title", "")[:200],
                raw={"via": "resolver-fallback"},
            ))

    enriched_data: Optional[dict] = None
    if do_enrich and enricher is not None and resolved_url:
        # Run enrichment via v1 (sets profile.linkedin_enriched on success,
        # updates profile.linkedin_url). We copy the result.data if successful.
        profile.linkedin_url = resolved_url
        res = enricher.enrich_profile(profile)
        if res.success:
            enriched_data = res.data
            log.append(f"enrichlayer ok: {resolved_url}")
        else:
            log.append(f"enrichlayer rejected: {res.error}")

    return LinkedInResult(
        linkedin_hits=linkedin_hits,
        aux_hits=aux_hits,
        log=log,
        resolver_confidence=confidence,
        resolver_error=resolver_error,
        enriched_data=enriched_data,
    )


def _url_match(a: str, b: str) -> bool:
    def norm(u):
        return (u or "").lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return norm(a) == norm(b)
