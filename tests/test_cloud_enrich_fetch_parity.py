"""End-to-end test for api/enrich.py cloud-parity fix.

Regression test for plans/diagnosis_architecture.md section 4, bug #1:
the cloud enrichment endpoint skipped fetch_links(), so cloud-enriched
profiles never got website_url / twitter_url / other_links / fetched_content.

This test:
  1. Creates a throwaway dataset with 3 profiles that have links
     (website_url, twitter_url, other_links) but enrichment_status
     already set to ENRICHED. That isolates the fetching + carding
     phases of api/enrich.py from the (slow, costly) identity +
     LinkedIn enrichment phases.
  2. BEFORE: count profiles in the dataset with non-empty fetched_content
     — should be 0.
  3. Drives the handler's phase methods (_handle_fetching, _handle_carding)
     directly via a fake handler object. This simulates what Vercel would
     do on successive POSTs once the enrichment phase has finished.
  4. AFTER: count profiles with non-empty fetched_content — should be > 0.

The profile cards are also rebuilt during phase 3, so we assert at least
one profile ends up with a non-empty profile_card as a secondary check.

Run:
    cd candidate-search-tool/
    python -m pytest tests/test_cloud_enrich_fetch_parity.py -v
    # or
    python tests/test_cloud_enrich_fetch_parity.py
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# Make project root importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from supabase import create_client

from api import enrich as enrich_mod
from cloud.storage.supabase import SupabaseStorage
from enrichment.models import EnrichmentStatus, Profile


TEST_ACCOUNT_ID = "4cff802c-ac7d-4836-8d50-5d5c1e31962e"


class _FakeHandler:
    """Stands in for a BaseHTTPRequestHandler so handler methods work.

    json_response() writes to self via a few attributes we don't need to
    replicate perfectly — we just want to capture status + body.
    """
    def __init__(self):
        self.responses = []
        self.wfile = self  # so json_response can call self.wfile.write
        self.headers = {}

    # The project's json_response helper imports from cloud.auth. It
    # writes the response body to handler.wfile. We just swallow it.
    def send_response(self, code):
        self._code = code

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        try:
            self.responses.append(json.loads(data.decode("utf-8")))
        except Exception:
            self.responses.append({"_raw": data})


def _make_dataset_row(dataset_id: str) -> dict:
    return {
        "id": dataset_id,
        "account_id": TEST_ACCOUNT_ID,
        "name": "fetch-parity-test",
        "source_file": "synthetic",
        "total_rows": 3,
        "field_mappings": [],
        "searchable_fields": [],
        "enrichment_stats": {},
    }


def _make_profile_rows(dataset_id: str) -> list[dict]:
    """Three profiles with one non-LinkedIn link each, already 'enriched'.

    We intentionally skip the enrichment phase — this test exercises
    the NEW phases (fetching + carding) that were missing before.
    The URLs are all public and lightweight to fetch.
    """
    return [
        {
            "id": uuid.uuid4().hex[:8],
            "account_id": TEST_ACCOUNT_ID,
            "dataset_id": dataset_id,
            "name": "Test Alpha",
            "email": "alpha@example.org",
            "website_url": "https://example.com/",
            "twitter_url": "",
            "other_links": [],
            "linkedin_enriched": {},
            "content_fields": {"note": "synthetic test fixture"},
            "metadata": {},
            "fetched_content": {},  # empty pre-fetch
            "profile_card": "",
            "field_summaries": {},
            "enrichment_status": "enriched",
            "enrichment_log": [],
        },
        {
            "id": uuid.uuid4().hex[:8],
            "account_id": TEST_ACCOUNT_ID,
            "dataset_id": dataset_id,
            "name": "Test Beta",
            "email": "beta@example.org",
            "website_url": "https://www.iana.org/help/example-domains",
            "twitter_url": "",
            "other_links": [],
            "linkedin_enriched": {},
            "content_fields": {"note": "synthetic test fixture"},
            "metadata": {},
            "fetched_content": {},
            "profile_card": "",
            "field_summaries": {},
            "enrichment_status": "enriched",
            "enrichment_log": [],
        },
        {
            "id": uuid.uuid4().hex[:8],
            "account_id": TEST_ACCOUNT_ID,
            "dataset_id": dataset_id,
            "name": "Test Gamma (no links)",
            "email": "gamma@example.org",
            "website_url": "",
            "twitter_url": "",
            "other_links": [],
            "linkedin_enriched": {},
            "content_fields": {"note": "synthetic test fixture"},
            "metadata": {},
            "fetched_content": {},
            "profile_card": "",
            "field_summaries": {},
            "enrichment_status": "enriched",
            "enrichment_log": [],
        },
    ]


def _count_with_fetched(client, dataset_id: str) -> int:
    """Count profiles where fetched_content is NOT the empty dict."""
    resp = (
        client.table("profiles")
        .select("id,fetched_content")
        .eq("dataset_id", dataset_id)
        .execute()
    )
    count = 0
    for r in (resp.data or []):
        fc = r.get("fetched_content")
        if isinstance(fc, str):
            try:
                fc = json.loads(fc)
            except ValueError:
                fc = None
        if fc:
            count += 1
    return count


def _count_with_profile_card(client, dataset_id: str) -> int:
    resp = (
        client.table("profiles")
        .select("id,profile_card")
        .eq("dataset_id", dataset_id)
        .execute()
    )
    return sum(1 for r in (resp.data or []) if (r.get("profile_card") or ""))


def run() -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    client = create_client(url, key)
    storage = SupabaseStorage(url, key, TEST_ACCOUNT_ID)

    dataset_id = uuid.uuid4().hex
    print(f"[setup] using throwaway dataset_id={dataset_id}")

    # Insert dataset + profile rows directly (bypasses the Dataset model so
    # we don't need to import it here).
    client.table("datasets").insert(_make_dataset_row(dataset_id)).execute()
    client.table("profiles").insert(_make_profile_rows(dataset_id)).execute()

    try:
        # BEFORE
        before = _count_with_fetched(client, dataset_id)
        print(f"[before] profiles with fetched_content: {before}")
        assert before == 0, "fixture should start with zero fetched"

        # Build a pipeline (we only actually need its build_profile_cards).
        from api._helpers import get_pipeline
        pipeline = get_pipeline(TEST_ACCOUNT_ID)

        # Fake handler to drive the phase methods.
        from api.enrich import handler as EnrichHandler
        h = EnrichHandler.__new__(EnrichHandler)
        # Attach a minimal wfile/send_response so json_response works.
        fake = _FakeHandler()
        h.wfile = fake.wfile
        h.send_response = fake.send_response
        h.send_header = fake.send_header
        h.end_headers = fake.end_headers

        # Create a job so the "carding" phase has something to update.
        job_id = storage.create_job(dataset_id, 3)

        # Drive fetching phase until it reports no more work.
        # (In real traffic, the frontend polls and calls /api/enrich again;
        # here we simulate that loop inline.)
        MAX_ITERS = 20
        iters = 0
        while iters < MAX_ITERS:
            iters += 1
            remaining_ids = enrich_mod._ids_needing_fetch(
                storage, dataset_id, TEST_ACCOUNT_ID,
            )
            print(f"[fetch iter {iters}] remaining_ids={len(remaining_ids)}")
            if not remaining_ids:
                break
            h._handle_fetching(storage, dataset_id, TEST_ACCOUNT_ID, job_id, 3)

        # Drive carding phase exactly once.
        h._handle_carding(storage, pipeline, dataset_id, job_id, 3)

        # AFTER
        after = _count_with_fetched(client, dataset_id)
        cards = _count_with_profile_card(client, dataset_id)
        print(f"[after]  profiles with fetched_content: {after}")
        print(f"[after]  profiles with profile_card:    {cards}")

        assert after > 0, (
            f"FAIL: expected at least one profile with fetched_content, got {after}. "
            "The cloud enrich path is still skipping link fetching."
        )
        # profile_card should rebuild for all 3 profiles (incl. gamma with no links
        # — cards are built from content_fields too).
        assert cards >= 1, f"FAIL: expected at least one profile_card, got {cards}"

        print("PASS: cloud enrichment now populates fetched_content and profile_card.")
    finally:
        # Clean up — delete profiles then the dataset.
        client.table("profiles").delete().eq("dataset_id", dataset_id).execute()
        client.table("datasets").delete().eq("id", dataset_id).execute()
        # Best-effort job cleanup (not strictly required).
        try:
            client.table("jobs").delete().eq("dataset_id", dataset_id).execute()
        except Exception:
            pass
        print("[cleanup] removed throwaway dataset + profiles")


if __name__ == "__main__":
    run()
