"""Coverage report for an enriched dataset.

Analyzes profile state produced by the enrichment pipeline and reports:
  - status counts (coverage table)
  - cohort breakdown (email type, org presence, name length)
  - source contribution (LinkedIn / website / twitter / other / fetched)
  - enrichment version breakdown
  - cost estimate (from log pattern counts)
  - log pattern analysis (failure strings)

Reads existing data — does NOT re-run the enrichment pipeline.

CLI:
    python -m enrichment.eval.coverage_report \\
        --account-id <uuid> --dataset-id <id>

    python -m enrichment.eval.coverage_report --local path/to/dataset.json

Both paths print a human-readable report AND return a structured dict
when used as a library:

    from enrichment.eval.coverage_report import run_report
    report = run_report(profiles)
    print(report["coverage"])
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
from typing import Any, Iterable

# Ensure project root on sys.path for direct-script invocation
_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from enrichment.identity import PERSONAL_DOMAINS  # noqa: E402
from enrichment.models import Dataset, EnrichmentStatus, Profile  # noqa: E402


# Pricing — mirrors enrichment/costs.py defaults.
LINKEDIN_UNIT_COST = 0.0168      # per EnrichLayer call
SEARCH_UNIT_COST = 0.006         # Brave + Serper combined per query (plans/diagnosis)


# Log patterns worth counting. Keys become report buckets; values are
# substring matches. First-match-wins; every log line increments at most
# one bucket (aside from cost counters, which are tallied separately).
LOG_PATTERNS: list[tuple[str, str]] = [
    ("search_query", "Search ("),
    ("trying_linkedin", "Trying LinkedIn"),
    ("verify_accepted", "Verify result: ACCEPTED"),
    ("verify_rejected", "Verify result: REJECTED"),
    ("weak_match_rejected", "WEAK-MATCH REJECTED"),
    ("org_mismatch", "Verify org: MISMATCH"),
    ("org_mismatch_generic", "MISMATCH"),
    ("no_linkedin_found", "No LinkedIn profile found"),
    ("verification_failed", "verification failed"),
    ("out_of_credits", "Out of API credits"),
    ("out_of_credits_alt", "OUT_OF_CREDITS"),
    ("enrichlayer_error", "LinkedIn enrichment failed"),
]


# ────────────────────────────────────────────────────────────────────
# Cohort classification helpers
# ────────────────────────────────────────────────────────────────────


def _email_type(email: str) -> str:
    """Classify an email into a coarse cohort."""
    if not email:
        return "no_email"
    e = email.lower().strip()
    dom = e.split("@", 1)[-1] if "@" in e else ""
    if not dom:
        return "malformed"
    if dom in PERSONAL_DOMAINS:
        return "personal"
    if dom.endswith(".edu"):
        return "edu"
    if dom.endswith(".gov"):
        return "gov"
    if dom.endswith(".org"):
        return "org"
    return "corp"


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


# ────────────────────────────────────────────────────────────────────
# Source contribution
# ────────────────────────────────────────────────────────────────────


def _source_presence(p: Profile) -> dict[str, bool]:
    """Which signal sources are populated on this profile?"""
    return {
        "linkedin": bool(p.linkedin_enriched),
        "website": bool(p.website_url),
        "twitter": bool(p.twitter_url),
        "resume": bool(p.resume_url),
        "other_links": bool(p.other_links),
        "fetched_content": bool(p.fetched_content),
        "profile_card": bool(p.profile_card),
    }


# ────────────────────────────────────────────────────────────────────
# Log-pattern analysis and cost estimation
# ────────────────────────────────────────────────────────────────────


_LI_ENRICH_RE = re.compile(r"Trying LinkedIn:")
_SEARCH_RE = re.compile(r"Search \(")


def _count_log_events(log: Iterable[str]) -> Counter:
    """Count the log-pattern buckets for a single profile's log."""
    out: Counter = Counter()
    for line in log or []:
        s = str(line)
        for name, needle in LOG_PATTERNS:
            if needle in s:
                out[name] += 1
                # don't break — a line can match multiple named patterns on
                # purpose; we use first-needle dedupe for cost counters only
    return out


def _cost_for_profile(log: Iterable[str]) -> tuple[float, int, int]:
    """Estimate (cost, searches, linkedin_calls) for one profile.

    A 'search' is any `Search (...)` log line. A 'linkedin call' is any
    `Trying LinkedIn:` line (each is a real EnrichLayer hit).
    """
    searches = 0
    linkedin_calls = 0
    for line in log or []:
        s = str(line)
        if _SEARCH_RE.search(s):
            searches += 1
        if _LI_ENRICH_RE.search(s):
            linkedin_calls += 1
    cost = searches * SEARCH_UNIT_COST + linkedin_calls * LINKEDIN_UNIT_COST
    return cost, searches, linkedin_calls


# ────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────


def run_report(profiles: list[Profile]) -> dict[str, Any]:
    """Compute the structured report dict for a list of profiles."""
    total = len(profiles)
    status_counts: Counter = Counter()
    version_counts: Counter = Counter()

    cohort_by_email: dict[str, Counter] = defaultdict(Counter)
    cohort_by_org: dict[str, Counter] = defaultdict(Counter)
    cohort_by_name_len: dict[str, Counter] = defaultdict(Counter)

    source_among_enriched: Counter = Counter()
    source_among_all: Counter = Counter()
    log_event_totals: Counter = Counter()

    per_profile_cost: list[float] = []
    per_profile_searches: list[int] = []
    per_profile_linkedin_calls: list[int] = []

    enriched_count = 0

    for p in profiles:
        status = p.enrichment_status.value if isinstance(p.enrichment_status, EnrichmentStatus) else str(p.enrichment_status)
        status_counts[status] += 1
        version_counts[p.enrichment_version or "(none)"] += 1

        # cohort breakdowns
        et = _email_type(p.email)
        cohort_by_email[et][status] += 1
        cohort_by_org[_has_org(p)][status] += 1
        cohort_by_name_len[_name_bucket(p)][status] += 1

        # source presence
        sp = _source_presence(p)
        for k, v in sp.items():
            if v:
                source_among_all[k] += 1
        if status == EnrichmentStatus.ENRICHED.value:
            enriched_count += 1
            for k, v in sp.items():
                if v:
                    source_among_enriched[k] += 1

        # log events + cost
        log_event_totals.update(_count_log_events(p.enrichment_log))
        cost, searches, li = _cost_for_profile(p.enrichment_log)
        per_profile_cost.append(cost)
        per_profile_searches.append(searches)
        per_profile_linkedin_calls.append(li)

    def _pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 1) if d else 0.0

    coverage = {
        "total": total,
        "status_counts": dict(status_counts),
        "status_pct": {k: _pct(v, total) for k, v in status_counts.items()},
    }

    def _cohort_block(counter_map: dict[str, Counter]) -> dict[str, dict]:
        out = {}
        for cohort, ctr in counter_map.items():
            sub_total = sum(ctr.values())
            out[cohort] = {
                "total": sub_total,
                "status_counts": dict(ctr),
                "enriched_pct": _pct(ctr.get("enriched", 0), sub_total),
            }
        return out

    cohorts = {
        "email_type": _cohort_block(cohort_by_email),
        "org_presence": _cohort_block(cohort_by_org),
        "name_length": _cohort_block(cohort_by_name_len),
    }

    source_contribution = {
        "enriched_total": enriched_count,
        "among_enriched_counts": dict(source_among_enriched),
        "among_enriched_pct": {
            k: _pct(source_among_enriched.get(k, 0), enriched_count)
            for k in sorted(set(source_among_enriched) | {"linkedin", "website", "twitter", "other_links", "fetched_content", "profile_card"})
        },
        "among_all_counts": dict(source_among_all),
    }

    total_cost = sum(per_profile_cost)
    total_searches = sum(per_profile_searches)
    total_linkedin_calls = sum(per_profile_linkedin_calls)
    cost_block = {
        "estimated_total_usd": round(total_cost, 4),
        "mean_per_profile_usd": round(total_cost / total, 4) if total else 0.0,
        "total_search_queries": total_searches,
        "total_linkedin_api_calls": total_linkedin_calls,
        "search_unit_cost": SEARCH_UNIT_COST,
        "linkedin_unit_cost": LINKEDIN_UNIT_COST,
    }
    if per_profile_cost:
        cost_block["p50_per_profile_usd"] = round(statistics.median(per_profile_cost), 4)
        if len(per_profile_cost) >= 20:
            sorted_costs = sorted(per_profile_cost)
            idx = max(0, int(round(0.95 * (len(sorted_costs) - 1))))
            cost_block["p95_per_profile_usd"] = round(sorted_costs[idx], 4)

    return {
        "coverage": coverage,
        "cohorts": cohorts,
        "source_contribution": source_contribution,
        "enrichment_versions": dict(version_counts),
        "cost": cost_block,
        "log_events": dict(log_event_totals),
        "_integrity": {
            "status_counts_sum": sum(status_counts.values()),
            "matches_total": sum(status_counts.values()) == total,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Human-readable formatting
# ────────────────────────────────────────────────────────────────────


def _fmt_table(rows: list[tuple], headers: tuple[str, ...]) -> list[str]:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    out = []
    sep = "  "
    fmt = sep.join("{:<" + str(w) + "}" for w in widths)
    out.append(fmt.format(*headers))
    out.append(sep.join("-" * w for w in widths))
    for r in rows:
        out.append(fmt.format(*[str(c) for c in r]))
    return out


def format_report(report: dict[str, Any], *, label: str = "") -> str:
    """Render the structured report as human-readable text."""
    lines: list[str] = []
    bar = "=" * 72

    lines.append(bar)
    lines.append(f"COVERAGE REPORT{(' — ' + label) if label else ''}")
    lines.append(bar)

    # ─ coverage
    cov = report["coverage"]
    total = cov["total"]
    lines.append(f"\nTotal profiles: {total}")
    integrity = report.get("_integrity", {})
    if not integrity.get("matches_total", True):
        lines.append(
            f"  WARNING: status counts sum {integrity.get('status_counts_sum')} != total {total}"
        )

    rows = [
        (status, cov["status_counts"].get(status, 0), f"{cov['status_pct'].get(status, 0.0)}%")
        for status in ("enriched", "pending", "skipped", "failed")
        if status in cov["status_counts"]
    ]
    # catch any other statuses that showed up
    for status in cov["status_counts"]:
        if status not in {"enriched", "pending", "skipped", "failed"}:
            rows.append((status, cov["status_counts"][status], f"{cov['status_pct'].get(status, 0.0)}%"))
    lines.append("\nStatus distribution:")
    lines.extend("  " + ln for ln in _fmt_table(rows, ("status", "count", "pct")))

    # ─ cohorts
    lines.append("\nCohort breakdown (% enriched within each cohort):")
    for cohort_name, cohort_data in report["cohorts"].items():
        lines.append(f"\n  by {cohort_name}:")
        rows = []
        for bucket, stats in sorted(cohort_data.items(), key=lambda kv: -kv[1]["total"]):
            rows.append(
                (
                    bucket,
                    stats["total"],
                    stats["status_counts"].get("enriched", 0),
                    stats["status_counts"].get("skipped", 0),
                    stats["status_counts"].get("failed", 0),
                    f"{stats['enriched_pct']}%",
                )
            )
        lines.extend(
            "    " + ln
            for ln in _fmt_table(rows, ("bucket", "total", "enriched", "skipped", "failed", "enriched_pct"))
        )

    # ─ source contribution
    sc = report["source_contribution"]
    lines.append(f"\nSource contribution (among {sc['enriched_total']} enriched profiles):")
    rows = []
    for src in ("linkedin", "website", "twitter", "other_links", "fetched_content", "resume", "profile_card"):
        count = sc["among_enriched_counts"].get(src, 0)
        pct = sc["among_enriched_pct"].get(src, 0.0)
        rows.append((src, count, f"{pct}%"))
    lines.extend("  " + ln for ln in _fmt_table(rows, ("source", "count", "pct_of_enriched")))

    # ─ enrichment versions
    vers = report["enrichment_versions"]
    lines.append("\nEnrichment version distribution:")
    rows = sorted(((k, v) for k, v in vers.items()), key=lambda kv: -kv[1])
    lines.extend("  " + ln for ln in _fmt_table(rows, ("version", "count")))

    # ─ cost
    cost = report["cost"]
    lines.append("\nCost (estimated from enrichment_log):")
    lines.append(f"  total: ${cost['estimated_total_usd']}")
    lines.append(f"  mean/profile: ${cost['mean_per_profile_usd']}")
    if "p50_per_profile_usd" in cost:
        lines.append(f"  p50/profile: ${cost['p50_per_profile_usd']}")
    if "p95_per_profile_usd" in cost:
        lines.append(f"  p95/profile: ${cost['p95_per_profile_usd']}")
    lines.append(
        f"  totals: {cost['total_search_queries']} search queries × ${cost['search_unit_cost']} + "
        f"{cost['total_linkedin_api_calls']} LinkedIn calls × ${cost['linkedin_unit_cost']}"
    )

    # ─ log events
    le = report["log_events"]
    lines.append("\nLog-pattern counts (across all profiles):")
    rows = sorted(((k, v) for k, v in le.items()), key=lambda kv: -kv[1])
    lines.extend("  " + ln for ln in _fmt_table(rows, ("pattern", "count")))

    lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# CLI / loaders
# ────────────────────────────────────────────────────────────────────


def _load_local(path: str) -> tuple[list[Profile], str]:
    ds = Dataset.load(Path(path))
    label = f"local:{ds.name}"
    return ds.profiles, label


def _load_cloud(account_id: str, dataset_id: str) -> tuple[list[Profile], str]:
    # Lazy imports so the local path works without supabase installed.
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore

    if load_dotenv:
        # .env sits at repo root; best-effort load
        for p in (_root / ".env", Path.cwd() / ".env"):
            if p.exists():
                load_dotenv(str(p))
                break

    from cloud.storage.supabase import SupabaseStorage  # noqa: E402

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY must be set in env (or .env) "
            "for cloud reports."
        )
    storage = SupabaseStorage(url, key, account_id)
    ds = storage.load_dataset(dataset_id)
    label = f"cloud:{ds.name} ({dataset_id})"
    return ds.profiles, label


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.eval.coverage_report",
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--account-id", help="Supabase account_id (cloud mode)")
    parser.add_argument("--dataset-id", help="Dataset id (cloud or local filename stem)")
    parser.add_argument("--local", help="Path to a local Dataset JSON file")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of text")
    parser.add_argument("--out", help="Write the text report to this path (in addition to stdout)")
    args = parser.parse_args(argv)

    if args.local:
        profiles, label = _load_local(args.local)
    elif args.account_id and args.dataset_id:
        profiles, label = _load_cloud(args.account_id, args.dataset_id)
    else:
        parser.error("Provide either --local <path> or both --account-id and --dataset-id")

    report = run_report(profiles)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        text = format_report(report, label=label)
        print(text)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
