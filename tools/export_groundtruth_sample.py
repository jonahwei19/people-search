"""Export a stratified sample of profiles to a CSV for hand-labeling.

The user fills in `true_linkedin_url`, `true_website_url`, `true_twitter_url`,
and `true_is_hidden`. The resulting CSV is the benchmark every pipeline change
is measured against (see `enrichment/eval/groundtruth.py`).

Usage:
    python -m tools.export_groundtruth_sample \\
        --account-id <id> --dataset-id <id> --n 50 \\
        --out plans/groundtruth_<dataset>.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from supabase import create_client


PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "me.com",
    "icloud.com", "aol.com", "protonmail.com", "proton.me", "msn.com",
    "live.com", "mail.com", "ymail.com", "zoho.com", "hey.com", "pm.me",
}


def _cohort(row: dict) -> str:
    email = (row.get("email") or "").lower()
    org = (row.get("organization") or "").strip()
    domain = email.split("@", 1)[1] if "@" in email else ""
    if not email:
        email_type = "no_email"
    elif domain in PERSONAL_DOMAINS:
        email_type = "personal"
    elif domain.endswith(".edu"):
        email_type = "edu"
    elif domain.endswith(".gov"):
        email_type = "gov"
    else:
        email_type = "corp"
    has_org = "org" if org else "noorg"
    return f"{email_type}+{has_org}"


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-id", required=True)
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    # Load all profiles in the dataset
    resp = (
        client.table("profiles")
        .select("id,name,email,organization,title,linkedin_url,enrichment_status")
        .eq("account_id", args.account_id)
        .eq("dataset_id", args.dataset_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        print(f"No profiles found in dataset {args.dataset_id}", file=sys.stderr)
        return 1

    # Bucket by cohort
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(_cohort(r), []).append(r)

    # Stratified sample
    rng = random.Random(args.seed)
    per_bucket = max(1, args.n // len(buckets))
    sample: list[dict] = []
    for key, pool in buckets.items():
        take = min(per_bucket, len(pool))
        sample.extend(rng.sample(pool, take))
    # Top up or trim to exactly n
    if len(sample) < args.n:
        leftover = [r for r in rows if r not in sample]
        rng.shuffle(leftover)
        sample.extend(leftover[: args.n - len(sample)])
    sample = sample[: args.n]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "profile_id", "uploaded_name", "email", "organization", "title",
            "current_linkedin_url", "current_enrichment_status",
            "true_linkedin_url", "true_website_url", "true_twitter_url",
            "true_is_hidden", "notes",
        ])
        for r in sample:
            w.writerow([
                r["id"], r.get("name", ""), r.get("email", ""),
                r.get("organization", ""), r.get("title", ""),
                r.get("linkedin_url", ""), r.get("enrichment_status", ""),
                "", "", "", "", "",  # blanks for human to fill
            ])

    # Print stratification report
    print(f"Wrote {len(sample)} rows to {out_path}", file=sys.stderr)
    print("Stratification:", file=sys.stderr)
    cohort_counts: dict[str, int] = {}
    for r in sample:
        cohort_counts[_cohort(r)] = cohort_counts.get(_cohort(r), 0) + 1
    for k in sorted(cohort_counts):
        print(f"  {k}: {cohort_counts[k]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
