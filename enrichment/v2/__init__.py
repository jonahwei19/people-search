"""Source-agnostic enrichment pipeline (Variant A+).

Stages:
    1. cohort.py            fast signals (email cohort, org domain, name slugs)
    2. org_site.py          crawl the profile's own email domain for team pages
    3. vertical_openalex.py OpenAlex API (scholars — free)
       vertical_github.py    GitHub user API (devs — free)
       vertical_substack.py  Substack search (writers — free)
    4. linkedin_resolve.py  v1 IdentityResolver + EnrichLayer, anchor-aware
    5. open_web.py          Brave/Serper fallback with tight two-anchor rule
    6. verify.py            two-anchor verifier + profile writer
    evidence.py             Evidence dataclass (produced by every stage)
    orchestrator.py         chains the stages with skip-when-satisfied

Entrypoint:
    from enrichment.v2 import run_v2
"""

from .cohort import classify_profile, CohortSignals
from .evidence import Evidence, merge_evidence, strong_anchors_count
from .orchestrator import run_profile_v2, run_v2, V2Budget, V2ProfileResult
from .verify import verify, write_profile, VerifyResult

__all__ = [
    "classify_profile",
    "CohortSignals",
    "Evidence",
    "merge_evidence",
    "strong_anchors_count",
    "run_profile_v2",
    "run_v2",
    "V2Budget",
    "V2ProfileResult",
    "verify",
    "write_profile",
    "VerifyResult",
]
