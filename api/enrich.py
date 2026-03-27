"""POST /api/enrich — Chunked enrichment.

Each invocation processes a batch of profiles and returns progress.
The frontend loops, calling this endpoint until status is "done".
Progress is persisted in Supabase so enrichment survives function restarts.

Request:  {"dataset_id": "...", "job_id": "..." (optional, omit for first call)}
Response: {"job_id": "...", "status": "running"|"done"|"error", "current": N, "total": N}
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, read_json_body,
    get_pipeline, get_storage,
)
from enrichment.models import EnrichmentStatus

CHUNK_SIZE = 50  # profiles per invocation (parallel within chunk)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        dataset_id = body.get("dataset_id")
        job_id = body.get("job_id")

        if not dataset_id:
            json_response(self, 400, {"error": "dataset_id required"})
            return

        aid = account["account_id"]
        storage = get_storage(aid)
        pipeline = get_pipeline(aid)

        try:
            dataset = storage.load_dataset(dataset_id)
        except Exception:
            json_response(self, 404, {"error": "Dataset not found"})
            return

        total = len(dataset.profiles)

        # Create or resume job
        if not job_id:
            job_id = storage.create_job(dataset_id, total)

        pending = [p for p in dataset.profiles if p.enrichment_status == EnrichmentStatus.PENDING]

        # All profiles processed — do final steps
        if not pending:
            try:
                # Fetch links for profiles that have them
                pipeline.fetch_links(dataset)
                # Build profile cards
                pipeline.build_profile_cards(dataset)
                storage.save_dataset(dataset)
                storage.update_job(job_id, status="done", current_count=total,
                                   message="Done")
            except Exception as e:
                storage.update_job(job_id, status="error", message=str(e))
                json_response(self, 200, {"job_id": job_id, "status": "error",
                                          "current": total - len(pending),
                                          "total": total, "message": str(e)})
                return

            json_response(self, 200, {
                "job_id": job_id, "status": "done",
                "current": total, "total": total,
            })
            return

        # Process a chunk of pending profiles
        chunk = pending[:CHUNK_SIZE]

        try:
            # Resolve identities (email → LinkedIn URL) — 20 parallel workers
            pipeline.resolver.resolve_batch(chunk, max_workers=20)

            # Enrich LinkedIn profiles — 20 parallel workers
            pipeline.enricher.enrich_batch(chunk, max_workers=20)

            # Save only the chunk we just processed (not all profiles)
            storage.save_profiles(dataset.id, chunk)

            done_count = sum(
                1 for p in dataset.profiles
                if p.enrichment_status != EnrichmentStatus.PENDING
            )
            storage.update_job(
                job_id,
                current_count=done_count,
                message=f"Enriched {done_count}/{total}",
            )

            json_response(self, 200, {
                "job_id": job_id,
                "status": "running",
                "current": done_count,
                "total": total,
            })
        except Exception as e:
            storage.update_job(job_id, status="error", message=str(e))
            json_response(self, 200, {
                "job_id": job_id, "status": "error",
                "current": total - len(pending), "total": total,
                "message": str(e),
            })

    def log_message(self, format, *args):
        pass
