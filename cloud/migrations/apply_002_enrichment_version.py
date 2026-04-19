"""Apply cloud/migrations/002_enrichment_version.sql to the live Supabase DB.

Why this exists: the Supabase Python client can't run arbitrary DDL, and the
direct-DB host (db.<project>.supabase.co) resolves only over IPv6 on many
networks, so psycopg2 can't reach it either. We split the migration into
two phases:

  1. DDL (ALTER TABLE) — must be pasted into the Supabase SQL Editor.
     This script prints the exact SQL and links to the editor.
  2. Back-fill (UPDATE) — runs fine over PostgREST once the column exists.
     This script does it automatically.

Run after pasting the ALTER statement in the SQL editor:

    python3 cloud/migrations/apply_002_enrichment_version.py

Idempotent: safe to re-run. Reports before/after counts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("pip install python-dotenv", file=sys.stderr)
    raise

from supabase import create_client

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

SQL_PATH = Path(__file__).parent / "002_enrichment_version.sql"

SUPABASE_URL = os.environ["SUPABASE_URL"]
PROJECT_REF = SUPABASE_URL.replace("https://", "").split(".")[0]
EDITOR_URL = f"https://supabase.com/dashboard/project/{PROJECT_REF}/sql/new"


def _column_exists(client) -> bool:
    try:
        client.table("profiles").select("enrichment_version").limit(1).execute()
        return True
    except Exception as e:
        if "enrichment_version" in str(e) and "does not exist" in str(e):
            return False
        raise


def _counts(client) -> dict:
    def n(filter_kind, value):
        q = client.table("profiles").select("id", count="exact")
        q = q.eq("enrichment_version", value) if filter_kind == "eq" else q.is_(
            "enrichment_version", "null"
        )
        return q.execute().count

    return {
        "total": client.table("profiles").select("id", count="exact").execute().count,
        "v1": n("eq", "v1"),
        "v0-legacy": n("eq", "v0-legacy"),
        "empty": n("eq", ""),
        "null": n("null", None),
    }


def main() -> int:
    client = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_KEY"])

    if not _column_exists(client):
        print("Column `enrichment_version` does not exist yet.")
        print(f"Paste the SQL below into the SQL editor, then re-run this script:\n  {EDITOR_URL}\n")
        print("-" * 72)
        print(SQL_PATH.read_text())
        print("-" * 72)
        return 1

    print("Column exists. Before:", _counts(client))

    # Back-fill: stamp everything already enriched/skipped/failed that is
    # still unlabeled as v0-legacy. Only touches pre-versioning rows — rows
    # already tagged (v1, v2, etc.) are left alone.
    for status in ("enriched", "skipped", "failed"):
        # Two passes: NULL and empty string. PostgREST "in" operator works
        # on lists of values; we update each status separately to stay
        # within request size limits even on very large tables.
        for version_filter in ("", None):
            q = (
                client.table("profiles")
                .update({"enrichment_version": "v0-legacy"})
                .eq("enrichment_status", status)
            )
            q = q.eq("enrichment_version", "") if version_filter == "" else q.is_(
                "enrichment_version", "null"
            )
            q.execute()

    print("After: ", _counts(client))
    return 0


if __name__ == "__main__":
    sys.exit(main())
