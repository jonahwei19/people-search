"""Acceptance test for 002_enrichment_version migration.

Prints before/after counts and the ratio of v1 (newly-stamped) vs v0-legacy
(historical back-fill). After a correct migration:

  - v1 should equal only the profiles actually re-run under current code
    (expected 0 immediately after migration, before any re-enrichment)
  - v0-legacy should equal the count of historical enriched/skipped/failed
    rows that existed before the migration
  - Total v1 + v0-legacy + empty should equal total profiles
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

client = create_client(
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
)


def count(**filters):
    q = client.table("profiles").select("id", count="exact")
    for k, v in filters.items():
        if v is None:
            q = q.is_(k, "null")
        else:
            q = q.eq(k, v)
    return q.execute().count


def main() -> int:
    try:
        v1 = count(enrichment_version="v1")
    except Exception as e:
        print(f"FAIL: column does not exist yet. Error: {e}", file=sys.stderr)
        return 1

    legacy = count(enrichment_version="v0-legacy")
    empty = count(enrichment_version="")
    null_ = count(enrichment_version=None)
    total = client.table("profiles").select("id", count="exact").execute().count

    enriched = count(enrichment_status="enriched")
    skipped = count(enrichment_status="skipped")
    failed = count(enrichment_status="failed")
    pending = count(enrichment_status="pending")

    print(f"Total profiles:              {total}")
    print()
    print("By enrichment_version:")
    print(f"  v1:                        {v1}")
    print(f"  v0-legacy:                 {legacy}")
    print(f"  '' (empty):                {empty}")
    print(f"  NULL:                      {null_}")
    print()
    print("By enrichment_status:")
    print(f"  enriched:                  {enriched}")
    print(f"  skipped:                   {skipped}")
    print(f"  failed:                    {failed}")
    print(f"  pending:                   {pending}")
    print()

    # Invariants
    touched = enriched + skipped + failed
    tagged = v1 + legacy
    print(f"touched (enriched+skipped+failed): {touched}")
    print(f"tagged  (v1 + v0-legacy):          {tagged}")

    if tagged == touched:
        print("OK: every non-pending row carries a version tag.")
    else:
        print(
            f"WARN: {touched - tagged} non-pending rows still untagged "
            "(run apply_002_enrichment_version.py to back-fill)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
