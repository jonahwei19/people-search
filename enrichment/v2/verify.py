"""Stage 6: Verify + write.

Applies the TWO-ANCHOR RULE:
    - A profile is "enriched" iff it has ≥1 Evidence with ≥2 anchors,
      OR ≥2 Evidence objects each with ≥1 anchor (cumulative across
      different sources / kinds).
    - "thin" if it has ≥1 Evidence with ≥1 anchor but the above fails.
    - "hidden" if search stages ran successfully but produced no Evidence.
    - "failed" if an infrastructure error prevented any stage from running.

Then writes the verified Evidence onto the Profile, respecting:
    - NEVER overwrite user-provided organization or title. LinkedIn-sourced
      values go to enriched_organization / enriched_title.
    - website_url: set only from strong Evidence with kind="website" or "bio"
    - twitter_url: set from strong Evidence with kind="twitter"
    - other_links: append all non-primary strong+thin Evidence URLs (deduped)
    - fetched_content: populate with Evidence snippets under keys
      "evidence_<source>_<kind>:<url>" so downstream summarization sees them.

Terminal status:
    EnrichmentStatus.ENRICHED — two-anchor rule satisfied
    EnrichmentStatus.SKIPPED  — "hidden" person (no public footprint) — kept
                                in pipeline so profile_card still builds from
                                uploaded content
    EnrichmentStatus.FAILED   — infrastructure error
    (thin uses ENRICHED but logs a "thin" marker so reports can count it)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models import EnrichmentStatus, Profile
from .evidence import Evidence


# Terminal state returned by the verifier. We map this onto
# EnrichmentStatus plus a log marker that downstream tooling reads.
STATE_ENRICHED = "enriched"
STATE_THIN = "thin"
STATE_HIDDEN = "hidden"
STATE_FAILED = "failed"


@dataclass
class VerifyResult:
    state: str
    strong_evidence: list[Evidence]     # ≥2 anchors
    corroborating: list[Evidence]       # 1 anchor (cumulative)
    rejected: list[Evidence]            # <1 anchor or duplicate/invalid
    log: list[str]


def verify(evidence: list[Evidence], infra_error: bool = False) -> VerifyResult:
    """Apply the two-anchor rule, bucket Evidence objects.

    Two-anchor rule:
        - strong_evidence = Evidence with ≥2 anchors (single URL)
        - If strong_evidence is non-empty → ENRICHED.
        - Else if ≥2 corroborating (1-anchor) hits from DIFFERENT sources →
          ENRICHED (the sources cross-verify each other).
        - Else if ≥1 corroborating hit → THIN.
        - Else if infra_error → FAILED.
        - Else → HIDDEN.
    """
    if infra_error and not evidence:
        return VerifyResult(STATE_FAILED, [], [], [], ["infra error, no evidence"])

    strong: list[Evidence] = []
    weak: list[Evidence] = []
    rejected: list[Evidence] = []

    for e in evidence:
        if e.is_strong():
            strong.append(e)
        elif len(e.anchors) == 1:
            weak.append(e)
        else:
            rejected.append(e)

    log: list[str] = []
    log.append(f"verify: {len(strong)} strong, {len(weak)} weak, {len(rejected)} rejected")

    if strong:
        return VerifyResult(STATE_ENRICHED, strong, weak, rejected, log)

    # Check distinct-source corroboration
    weak_sources = {e.source for e in weak}
    if len(weak) >= 2 and len(weak_sources) >= 2:
        return VerifyResult(STATE_ENRICHED, [], weak, rejected, log + ["enriched via 2 distinct-source corroboration"])

    if weak:
        return VerifyResult(STATE_THIN, [], weak, rejected, log + ["thin"])

    return VerifyResult(STATE_HIDDEN, [], [], rejected, log + ["hidden"])


def write_profile(
    profile: Profile,
    result: VerifyResult,
    enriched_data: Optional[dict] = None,
) -> None:
    """Apply verified Evidence to the profile.

    Respects user-provided organization / title (never overwrites).
    Sets enrichment_status to ENRICHED / SKIPPED / FAILED accordingly.
    """
    winners = list(result.strong_evidence) + list(result.corroborating)

    # Dedupe by URL
    seen: set[str] = set()
    ordered: list[Evidence] = []
    for e in winners:
        key = _canon(e.url)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(e)

    # Populate profile fields — NEVER overwrite user-provided values,
    # except for primary URL slots that are empty.
    existing_links_lower = {l.lower().rstrip("/") for l in (profile.other_links or [])}
    for e in ordered:
        if e.kind == "linkedin":
            if not profile.linkedin_url:
                profile.linkedin_url = e.url
            # Enriched LinkedIn data written below
        elif e.kind == "twitter":
            if not profile.twitter_url:
                profile.twitter_url = e.url
            elif _canon(e.url) not in existing_links_lower:
                profile.other_links.append(e.url)
                existing_links_lower.add(_canon(e.url))
        elif e.kind in ("website", "bio", "listing"):
            if not profile.website_url:
                profile.website_url = e.url
            elif _canon(e.url) not in existing_links_lower:
                profile.other_links.append(e.url)
                existing_links_lower.add(_canon(e.url))
        elif e.kind in ("github", "substack", "profile"):
            if _canon(e.url) not in existing_links_lower:
                profile.other_links.append(e.url)
                existing_links_lower.add(_canon(e.url))

        # Snippet → fetched_content
        if e.snippet:
            confidence = "strong" if e.is_strong() else "thin"
            slot = f"evidence({confidence},{e.source}):{e.url[:80]}"
            profile.fetched_content[slot] = e.snippet

    # LinkedIn enriched data (if Stage 4 populated it) follows v1 rules:
    # NEVER overwrite user-provided organization / title.
    if enriched_data:
        profile.linkedin_enriched = enriched_data
        if not profile.name and enriched_data.get("full_name"):
            profile.name = enriched_data["full_name"]
        if enriched_data.get("current_company"):
            profile.enriched_organization = enriched_data["current_company"]
            if not profile.organization:
                profile.organization = enriched_data["current_company"]
        if enriched_data.get("current_title"):
            profile.enriched_title = enriched_data["current_title"]
            if not profile.title:
                profile.title = enriched_data["current_title"]

    # Status
    if result.state == STATE_ENRICHED:
        profile.enrichment_status = EnrichmentStatus.ENRICHED
    elif result.state == STATE_THIN:
        profile.enrichment_status = EnrichmentStatus.ENRICHED
        profile.enrichment_log.append("v2: thin (1 corroborating anchor)")
    elif result.state == STATE_HIDDEN:
        profile.enrichment_status = EnrichmentStatus.SKIPPED
        profile.enrichment_log.append("v2: hidden (no public footprint)")
    else:
        profile.enrichment_status = EnrichmentStatus.FAILED
        profile.enrichment_log.append("v2: failed (infra error)")

    # Serialize evidence to enrichment_log for debugging
    for e in ordered:
        profile.enrichment_log.append(
            f"v2 evidence[{e.source}/{e.kind}]: {e.url} anchors={sorted(e.anchors)}"
        )


def _canon(url: str) -> str:
    u = (url or "").strip().lower()
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")
