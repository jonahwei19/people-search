"""v2 Orchestrator — chain all stages for a single profile.

Order:
    Stage 1: cohort classification (free)
    Stage 2: org-site crawl (if non-personal email)
    Stage 3: vertical APIs (cohort-aware parallel fan-out)
    Stage 4: LinkedIn resolve + enrich (v1 resolver, EnrichLayer)
    Stage 5: open-web fallback (only if < 2 strong anchors so far)
    Stage 6: verify + write

Skip-when-satisfied:
    After Stage 2, if we already have ≥2 strong Evidence, skip Stage 3+4.
    After Stage 3, if we already have ≥2 strong Evidence, skip Stage 4.
    After Stage 4, if we already have ≥2 strong Evidence, skip Stage 5.

Cost & latency tracked per-profile in the return dict.
"""

from __future__ import annotations

import concurrent.futures
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..enrichers import LinkedInEnricher
from ..identity import IdentityResolver
from ..models import EnrichmentStatus, Profile
from .cohort import CohortSignals, classify_profile, COHORT_PERSONAL, COHORT_NO_EMAIL, COHORT_EDU
from .evidence import Evidence, merge_evidence, strong_anchors_count
from .linkedin_resolve import resolve_linkedin
from .open_web import query_open_web
from .org_site import crawl_org_site
from .verify import verify, write_profile
from .vertical_github import query_github
from .vertical_openalex import query_openalex
from .vertical_substack import query_substack


# Cost constants (per external call)
COST_ORG_SITE_PAGE = 0.0          # free HTTP fetch
COST_OPENALEX = 0.0               # free API
COST_GITHUB = 0.0                 # free API (unauth tier)
COST_SUBSTACK = 0.0               # free
COST_SEARCH_QUERY = 0.006         # Brave + Serper combined
COST_LINKEDIN_API = 0.0168        # EnrichLayer


@dataclass
class V2Budget:
    """Per-profile execution budget."""
    max_org_site_pages: int = 14
    max_vertical_queries: int = 4
    max_search_queries: int = 2
    enable_linkedin: bool = True
    enable_open_web: bool = True


@dataclass
class V2ProfileResult:
    """Per-profile trace for the v2 run."""
    profile_id: str
    state: str                  # enriched / thin / hidden / failed
    evidence_count: int
    strong_count: int
    cost_usd: float
    wall_seconds: float
    stages_run: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def run_profile_v2(
    profile: Profile,
    resolver: IdentityResolver,
    enricher: LinkedInEnricher,
    budget: Optional[V2Budget] = None,
    brave_api_key: Optional[str] = None,
    serper_api_key: Optional[str] = None,
    contact_email: str = "research@example.com",
) -> V2ProfileResult:
    """Run all v2 stages for one profile. Mutates profile in place."""
    budget = budget or V2Budget()
    t0 = time.monotonic()
    evidence_pile: list[Evidence] = []
    stages: list[str] = []
    log: list[str] = []
    cost = 0.0

    # ─ Stage 1: cohort
    signals = classify_profile(profile)
    stages.append("stage1")
    log.append(
        f"stage1 cohort={signals.cohort} org_domain={signals.org_domain} "
        f"slugs={signals.name_slugs[:3]}"
    )

    # ─ Stage 2: org-site (only if non-personal email cohort)
    if signals.cohort not in (COHORT_PERSONAL, COHORT_NO_EMAIL) and signals.org_domain:
        try:
            r = crawl_org_site(signals)
            cost += 0.0  # free
            stages.append("stage2_org_site")
            log.extend(r.log[:20])
            merge_evidence(evidence_pile, r.hits)
        except Exception as e:
            log.append(f"stage2 exception: {e}")

    # Skip-when-satisfied check
    if strong_anchors_count(evidence_pile) >= 2:
        log.append("skip-when-satisfied after stage2")
    else:
        # ─ Stage 3: verticals (parallel, cohort-aware)
        stages.append("stage3_verticals")
        vertical_results = _run_verticals_parallel(
            signals, contact_email=contact_email
        )
        for r in vertical_results:
            log.extend(r.log[:10])
            merge_evidence(evidence_pile, r.hits)

    # Stage 4: LinkedIn — skip if we already have ≥2 strong anchors
    enriched_data = None
    if strong_anchors_count(evidence_pile) < 2 and budget.enable_linkedin:
        stages.append("stage4_linkedin")
        try:
            li = resolve_linkedin(
                profile, signals, resolver, enricher,
                priors=evidence_pile, do_enrich=True,
            )
            log.extend(li.log[:30])
            merge_evidence(evidence_pile, li.linkedin_hits)
            merge_evidence(evidence_pile, li.aux_hits)
            if li.linkedin_hits:
                cost += COST_SEARCH_QUERY * 2   # rough: resolver runs ~2 searches
            if li.enriched_data:
                cost += COST_LINKEDIN_API
                enriched_data = li.enriched_data
        except Exception as e:
            log.append(f"stage4 exception: {e}")
    else:
        log.append("skipping stage4 (already verified or disabled)")

    # Stage 5: open-web fallback
    if strong_anchors_count(evidence_pile) < 2 and budget.enable_open_web:
        stages.append("stage5_open_web")
        try:
            r = query_open_web(
                signals,
                profile_org=profile.organization,
                brave_api_key=brave_api_key,
                serper_api_key=serper_api_key,
                max_queries=budget.max_search_queries,
            )
            log.extend(r.log[:10])
            cost += r.queries * COST_SEARCH_QUERY
            merge_evidence(evidence_pile, r.hits)
        except Exception as e:
            log.append(f"stage5 exception: {e}")

    # Stage 6: verify + write
    stages.append("stage6_verify")
    v = verify(evidence_pile)
    write_profile(profile, v, enriched_data=enriched_data)
    log.extend(v.log)

    # Wall-time & final trace
    wall = time.monotonic() - t0
    profile.enrichment_log.append(
        f"v2: state={v.state} strong={len(v.strong_evidence)} "
        f"corroborating={len(v.corroborating)} cost=${cost:.4f} wall={wall:.2f}s"
    )

    return V2ProfileResult(
        profile_id=profile.id,
        state=v.state,
        evidence_count=len(evidence_pile),
        strong_count=strong_anchors_count(evidence_pile),
        cost_usd=cost,
        wall_seconds=wall,
        stages_run=stages,
        log=log,
    )


def _run_verticals_parallel(
    signals: CohortSignals,
    contact_email: str = "research@example.com",
):
    """Run all verticals in parallel, returning a list of typed results."""
    tasks: list[tuple[str, Callable]] = []

    # OpenAlex — run for edu cohort primarily, but also any cohort with a name
    if signals.first and signals.last:
        tasks.append(("openalex", lambda: query_openalex(signals, mailto=contact_email)))
        tasks.append(("github", lambda: query_github(signals)))
        tasks.append(("substack", lambda: query_substack(signals)))

    results = []
    if not tasks:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): name for name, fn in tasks}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                # Return a shim with a log entry so upstream can see the failure
                class _Err:
                    hits = []
                    queries = 0
                    log = [f"vertical {futures[fut]} failed: {e}"]
                results.append(_Err())
    return results


def run_v2(
    profiles: list[Profile],
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    on_batch_save: Optional[Callable[[], None]] = None,
    max_workers: int = 5,
    enrichlayer_api_key: Optional[str] = None,
    brave_api_key: Optional[str] = None,
    serper_api_key: Optional[str] = None,
    budget: Optional[V2Budget] = None,
) -> dict:
    """Run the v2 pipeline over a batch of profiles.

    Mutates profiles in place. Returns aggregate stats.
    """
    budget = budget or V2Budget()
    resolver = IdentityResolver(brave_api_key=brave_api_key, serper_api_key=serper_api_key)
    enricher = LinkedInEnricher(api_key=enrichlayer_api_key)

    # Only touch profiles still pending (or otherwise not already enriched).
    queue = [p for p in profiles if p.enrichment_status == EnrichmentStatus.PENDING]

    stats = {
        "total": len(queue),
        "enriched": 0,
        "thin": 0,
        "hidden": 0,
        "failed": 0,
        "total_cost_usd": 0.0,
        "per_profile": [],
    }
    completed = [0]
    batch_save_every = 20

    def _do(p: Profile) -> V2ProfileResult:
        try:
            return run_profile_v2(
                p, resolver, enricher, budget=budget,
                brave_api_key=brave_api_key, serper_api_key=serper_api_key,
            )
        except Exception as e:
            p.enrichment_status = EnrichmentStatus.FAILED
            p.enrichment_log.append(f"v2 fatal: {e}")
            return V2ProfileResult(
                profile_id=p.id, state="failed", evidence_count=0,
                strong_count=0, cost_usd=0.0, wall_seconds=0.0, log=[str(e)],
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_do, p): p for p in queue}
        for fut in concurrent.futures.as_completed(futures):
            p = futures[fut]
            result = fut.result()
            completed[0] += 1

            stats["total_cost_usd"] += result.cost_usd
            stats[result.state] = stats.get(result.state, 0) + 1
            stats["per_profile"].append({
                "id": result.profile_id,
                "state": result.state,
                "strong": result.strong_count,
                "cost_usd": round(result.cost_usd, 4),
                "wall_seconds": round(result.wall_seconds, 2),
            })

            if on_progress:
                on_progress(completed[0], len(queue), f"v2 {p.display_name()} → {result.state}")

            if on_batch_save and completed[0] % batch_save_every == 0:
                on_batch_save()

    return stats
