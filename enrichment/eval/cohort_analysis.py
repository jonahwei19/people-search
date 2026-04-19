"""Cohort analysis for an enriched dataset.

Slices the profile population along several axes and emits per-cohort
hit-rates, cost, wrong-person rates, and top failure reasons. The output
is intended to drive targeted tuning — "where is the pipeline weakest,
and what does each cohort suggest as the next intervention?"

Cohort axes
-----------

- Email type:      edu / gov / corp / personal / org / no_email / malformed
- Org presence:    org_present / no_org
- Name length:     2-token / 3+token / single / no_name
- Crosses:         personal×has_org, personal×no_org, corp×has_org, etc.
- Input quality:   name+corp-email+org / name+personal-email+org /
                   name+email-no-org / no-email

Per-cohort metrics
------------------

- N, enrichment rate, skipped rate, failed rate
- Wrong-person rate (via `wrong_person_audit.audit_profile`)
- Cost per profile (mean + p50), from `enrichment_log` patterns
- Top 3 failure reasons (parsed from log patterns)

CLI
---

    python -m enrichment.eval.cohort_analysis \\
        --account-id <uuid> --dataset-id <id> [--out plans/cohort_…md]

    python -m enrichment.eval.cohort_analysis --local path/to/dataset.json

Also exposed as a library:

    from enrichment.eval.cohort_analysis import run_cohort_analysis
    report = run_cohort_analysis(profiles)
    print(format_markdown(report, label="Allan c773996b"))
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from enrichment.eval.coverage_report import (  # noqa: E402
    LINKEDIN_UNIT_COST,
    SEARCH_UNIT_COST,
    _email_type,
    _cost_for_profile,
)
from enrichment.eval.wrong_person_audit import audit_profile  # noqa: E402
from enrichment.models import Dataset, EnrichmentStatus, Profile  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Cohort classifiers
# ────────────────────────────────────────────────────────────────────


def _has_org(p: Profile) -> str:
    return "org_present" if (p.organization or "").strip() else "no_org"


def _name_bucket(p: Profile) -> str:
    n = (p.name or "").strip()
    if not n:
        return "no_name"
    tokens = [t for t in n.split() if t]
    if len(tokens) <= 1:
        return "single_token"
    if len(tokens) == 2:
        return "two_token"
    return "three_plus_token"


def _cross_email_org(p: Profile) -> str:
    """Cross of email type × org presence."""
    et = _email_type(p.email)
    has = "has-org" if (p.organization or "").strip() else "no-org"
    return f"{et}×{has}"


def _input_quality(p: Profile) -> str:
    """Classify into the input-quality buckets from diagnosis_hitrate.md."""
    et = _email_type(p.email)
    has_org = bool((p.organization or "").strip())
    has_email = et not in ("no_email", "malformed")

    if not has_email:
        return "no-email"
    if et in ("corp", "edu", "gov", "org"):
        return "name+corp-email+org" if has_org else "name+corp-email+no-org"
    if et == "personal":
        return "name+personal-email+org" if has_org else "name+personal-email+no-org"
    return f"other-{et}"


def _email_type_of_profile(p: Profile) -> str:
    return _email_type(p.email)


COHORT_CLASSIFIERS: dict[str, Callable[[Profile], str]] = {
    "email_type":       _email_type_of_profile,
    "org_presence":     _has_org,
    "name_length":      _name_bucket,
    "email_x_org":      _cross_email_org,
    "input_quality":    _input_quality,
}


# ────────────────────────────────────────────────────────────────────
# Failure-reason extraction
# ────────────────────────────────────────────────────────────────────


# Log substrings → bucket label. First match wins per log line (ordered
# from most specific to least).
FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("REJECTED (soft penalty without corroborating", "soft_penalty_no_positive"),
    ("REJECTED (weak name match with no corroborating", "weak_name_no_positive"),
    ("WEAK-MATCH REJECTED", "weak_match_hard_reject"),
    ("Verify name: MISMATCH", "name_mismatch"),
    ("Verify org: MISMATCH", "org_mismatch"),
    ("Verify location: MISMATCH", "location_mismatch"),
    ("Verify content relevance: WEAK", "content_weak"),
    ("Ambiguous:", "ambiguous_tie"),
    ("No LinkedIn profile found", "no_linkedin_found"),
    ("Best match too weak", "score_below_threshold"),
    ("API returned no data", "api_no_data"),
    ("LinkedIn enrichment failed", "enrichlayer_error"),
    ("Out of API credits", "out_of_credits"),
]


def _failure_reasons(log: Iterable[str]) -> list[str]:
    """Return the set of failure-reason buckets seen in this profile's log."""
    seen: set[str] = set()
    for raw in log or []:
        s = str(raw)
        for needle, bucket in FAILURE_PATTERNS:
            if needle in s:
                seen.add(bucket)
                break
    return sorted(seen)


# ────────────────────────────────────────────────────────────────────
# Core analysis
# ────────────────────────────────────────────────────────────────────


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _cohort_metrics(
    profiles: list[Profile],
) -> dict[str, Any]:
    """Compute the per-cohort metrics for a single bucket-list of profiles."""
    n = len(profiles)
    status_counts: Counter = Counter()
    costs: list[float] = []
    searches = 0
    li_calls = 0
    wrong_person = 0
    reason_counts: Counter = Counter()
    enriched_n = 0

    for p in profiles:
        st = p.enrichment_status.value if isinstance(p.enrichment_status, EnrichmentStatus) else str(p.enrichment_status)
        status_counts[st] += 1
        cost, s, l = _cost_for_profile(p.enrichment_log)
        costs.append(cost)
        searches += s
        li_calls += l

        if st == EnrichmentStatus.ENRICHED.value:
            enriched_n += 1
            suspect = audit_profile(p)
            if suspect and "single_token_uploaded_name" not in suspect.get("flags", []):
                wrong_person += 1

        # Failure reasons only relevant for failed/skipped profiles
        if st in (EnrichmentStatus.FAILED.value, EnrichmentStatus.SKIPPED.value):
            for r in _failure_reasons(p.enrichment_log):
                reason_counts[r] += 1

    metrics: dict[str, Any] = {
        "n": n,
        "status_counts": dict(status_counts),
        "enriched_pct": _pct(status_counts.get(EnrichmentStatus.ENRICHED.value, 0), n),
        "skipped_pct": _pct(status_counts.get(EnrichmentStatus.SKIPPED.value, 0), n),
        "failed_pct": _pct(status_counts.get(EnrichmentStatus.FAILED.value, 0), n),
        "pending_pct": _pct(status_counts.get(EnrichmentStatus.PENDING.value, 0), n),
        "wrong_person_in_enriched": wrong_person,
        "wrong_person_pct_of_enriched": _pct(wrong_person, enriched_n) if enriched_n else 0.0,
        "total_cost_usd": round(sum(costs), 4),
        "mean_cost_per_profile_usd": round(sum(costs) / n, 4) if n else 0.0,
        "p50_cost_per_profile_usd": round(statistics.median(costs), 4) if costs else 0.0,
        "total_search_queries": searches,
        "total_linkedin_calls": li_calls,
        "top_failure_reasons": reason_counts.most_common(5),
    }
    return metrics


def run_cohort_analysis(profiles: list[Profile]) -> dict[str, Any]:
    """Walk every cohort axis and collect per-bucket metrics.

    Includes:
      - `overall` — flat metrics across the whole dataset
      - one entry per cohort axis, mapping bucket → metrics
      - `integrity` — bucket-sum-equals-total checks
    """
    total = len(profiles)
    report: dict[str, Any] = {
        "total": total,
        "overall": _cohort_metrics(profiles),
        "cohorts": {},
        "integrity": {},
    }

    for axis, classifier in COHORT_CLASSIFIERS.items():
        buckets: dict[str, list[Profile]] = defaultdict(list)
        for p in profiles:
            buckets[classifier(p)].append(p)
        report["cohorts"][axis] = {
            bucket: _cohort_metrics(plist) for bucket, plist in buckets.items()
        }
        report["integrity"][axis] = {
            "sum_of_buckets": sum(len(v) for v in buckets.values()),
            "matches_total": sum(len(v) for v in buckets.values()) == total,
        }

    return report


# ────────────────────────────────────────────────────────────────────
# Recommendations
# ────────────────────────────────────────────────────────────────────


def _recommendations(report: dict[str, Any]) -> list[str]:
    """Produce cohort-specific, actionable recommendations.

    Heuristics are deliberately data-driven but interpretive — they
    inspect the report's own numbers to suggest where to look.
    """
    recs: list[str] = []
    cohorts = report["cohorts"]

    # Email type drill-down
    email_buckets = cohorts.get("email_type", {})
    for bucket, m in sorted(email_buckets.items(), key=lambda kv: -kv[1]["n"]):
        if m["n"] < 5:
            continue
        tag = f"**{bucket}** (N={m['n']}, enriched={m['enriched_pct']}%, "
        tag += f"cost/profile=${m['mean_cost_per_profile_usd']:.3f}): "
        if bucket == "edu" and m["enriched_pct"] < 50:
            recs.append(
                tag + "enriched rate is low. Academic faculty pages and "
                ".edu team listings often contain the LinkedIn URL in HTML but "
                "aren't being followed. Consider an `org-website` crawler or an "
                "OpenAlex lookup step before LinkedIn search. P3 dead-code fix "
                "is especially relevant here."
            )
        elif bucket == "gov" and m["enriched_pct"] >= 50:
            recs.append(
                tag + "gov cohort performs reasonably — org-site crawl pays "
                "off here because agency pages are public. Consider running "
                "this cohort with a dedicated gov-site crawler first."
            )
        elif bucket == "personal" and m["enriched_pct"] < 50:
            recs.append(
                tag + "personal-email cohort is hardest. The best lever is "
                "email-exact evidence-following on the few pages that mention "
                "the username. Consider tightening `email-username` scoring "
                "(P1) to de-poison common-first-name ties."
            )
        elif bucket == "corp":
            recs.append(
                tag + "corp-email cohort is best-case. Make sure the "
                "email-verified-org bonus fires — it should dominate scoring. "
                "If wrong-person rate is elevated, name-match strengthening "
                "(P2) helps more than scoring weights."
            )

    # Input quality drill-down
    iq_buckets = cohorts.get("input_quality", {})
    worst_iq = None
    worst_rate = 101.0
    for bucket, m in iq_buckets.items():
        if m["n"] < 5:
            continue
        if m["enriched_pct"] < worst_rate:
            worst_rate = m["enriched_pct"]
            worst_iq = (bucket, m)
    if worst_iq:
        bucket, m = worst_iq
        recs.append(
            f"**Hardest input-quality cohort: `{bucket}`** — "
            f"N={m['n']}, enriched={m['enriched_pct']}%. "
            "This is the segment to target with pipeline improvements; "
            "use it as the denominator for any A/B experiment guardrails."
        )

    # Org presence
    org_buckets = cohorts.get("org_presence", {})
    if "no_org" in org_buckets:
        m = org_buckets["no_org"]
        if m["n"] >= 5 and m["enriched_pct"] < 20:
            recs.append(
                f"**no-org cohort** — N={m['n']}, enriched={m['enriched_pct']}%. "
                "Org is the single strongest verification signal; without it "
                "the scoring leans entirely on name + slug. Consider pre-inferring "
                "org from email domain earlier, or surfacing this cohort for "
                "manual review instead of auto-enriching."
            )

    # Wrong-person hot spots
    for axis_name, axis in cohorts.items():
        for bucket, m in axis.items():
            if m["n"] >= 10 and m["wrong_person_pct_of_enriched"] >= 10.0:
                recs.append(
                    f"**Wrong-person hot spot: {axis_name}=`{bucket}`** — "
                    f"N={m['n']}, enriched={m['status_counts'].get('enriched', 0)}, "
                    f"wrong-person rate {m['wrong_person_pct_of_enriched']}%. "
                    "Audit this cohort's accepted matches before using the data "
                    "downstream; scoring adjustments (P2 strict threshold) would "
                    "likely help."
                )

    return recs


# ────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────


def _fmt_reasons(reasons: list[tuple[str, int]]) -> str:
    if not reasons:
        return "—"
    return ", ".join(f"{name}({cnt})" for name, cnt in reasons)


def format_markdown(report: dict[str, Any], *, label: str = "") -> str:
    """Render the cohort report as Markdown suitable for `plans/`."""
    total = report["total"]
    o = report["overall"]
    parts: list[str] = []
    parts.append(f"# Cohort Analysis{(' — ' + label) if label else ''}")
    parts.append("")
    parts.append(f"- Total profiles: **{total}**")
    parts.append(
        f"- Overall enriched: **{o['status_counts'].get('enriched', 0)} "
        f"({o['enriched_pct']}%)**"
    )
    parts.append(
        f"- Overall wrong-person rate (in enriched): "
        f"**{o['wrong_person_pct_of_enriched']}%** "
        f"({o['wrong_person_in_enriched']} flagged)"
    )
    parts.append(
        f"- Total cost (estimated from logs): **${o['total_cost_usd']:.2f}** "
        f"({o['total_search_queries']} searches + {o['total_linkedin_calls']} "
        f"LinkedIn calls)"
    )
    parts.append("")

    for axis_name, axis in report["cohorts"].items():
        parts.append(f"## By `{axis_name}`")
        parts.append("")
        parts.append(
            "| bucket | N | enriched% | skipped% | failed% | "
            "wrong-person% | $/profile | top failure reasons |"
        )
        parts.append(
            "|---|---|---|---|---|---|---|---|"
        )
        # Sort by N descending
        for bucket, m in sorted(axis.items(), key=lambda kv: -kv[1]["n"]):
            parts.append(
                f"| `{bucket}` | {m['n']} | {m['enriched_pct']}% | "
                f"{m['skipped_pct']}% | {m['failed_pct']}% | "
                f"{m['wrong_person_pct_of_enriched']}% | "
                f"${m['mean_cost_per_profile_usd']:.3f} | "
                f"{_fmt_reasons(m['top_failure_reasons'])} |"
            )
        integrity = report["integrity"].get(axis_name, {})
        if not integrity.get("matches_total", True):
            parts.append(
                f"\n> WARNING: bucket sum {integrity.get('sum_of_buckets')} "
                f"!= total {total}"
            )
        parts.append("")

    parts.append("## Recommendations")
    parts.append("")
    recs = _recommendations(report)
    if not recs:
        parts.append("No cohort triggers fired. Either the dataset is too "
                     "small or everything is already tuned.")
    else:
        for r in recs:
            parts.append(f"- {r}")
    parts.append("")
    parts.append(
        "## Method notes\n\n"
        "- Metrics computed from stored `enrichment_log`, not live API. "
        "Cost is an estimate (see `enrichment/eval/coverage_report.py`).\n"
        "- Wrong-person rate is a lower bound from "
        "`wrong_person_audit.audit_profile` (token-overlap + last-name "
        "heuristic; see that module for caveats).\n"
        "- Failure reasons are the set of pattern-matches on log lines, "
        "one-hot per profile. A profile with multiple failure modes counts "
        "once per bucket.\n"
    )
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────
# Loaders + CLI
# ────────────────────────────────────────────────────────────────────


def _load_local(path: str) -> tuple[list[Profile], str]:
    ds = Dataset.load(Path(path))
    return ds.profiles, f"local:{ds.name}"


def _load_cloud(account_id: str, dataset_id: str) -> tuple[list[Profile], str]:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore

    if load_dotenv:
        for p in (_root / ".env", Path.cwd() / ".env"):
            if p.exists():
                load_dotenv(str(p))
                break

    from cloud.storage.supabase import SupabaseStorage  # noqa: E402

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    storage = SupabaseStorage(url, key, account_id)
    ds = storage.load_dataset(dataset_id)
    return ds.profiles, f"cloud:{ds.name} ({dataset_id})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.eval.cohort_analysis",
        description="Per-cohort enrichment hit-rate + cost analysis.",
    )
    parser.add_argument("--account-id")
    parser.add_argument("--dataset-id")
    parser.add_argument("--local")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", help="Write Markdown to this path (in addition to stdout)")
    args = parser.parse_args(argv)

    if args.local:
        profiles, label = _load_local(args.local)
    elif args.account_id and args.dataset_id:
        profiles, label = _load_cloud(args.account_id, args.dataset_id)
    else:
        parser.error("Provide --local or --account-id + --dataset-id")

    report = run_cohort_analysis(profiles)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        md = format_markdown(report, label=label)
        print(md)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
