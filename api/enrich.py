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
    get_pipeline, get_storage, check_enrichment_keys,
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
        missing = check_enrichment_keys(aid)
        if missing:
            json_response(self, 400, {"error": f"Missing API keys: {', '.join(missing)}. Add them in Settings."})
            return
        storage = get_storage(aid)
        pipeline = get_pipeline(aid)

        try:
            # Only count total (lightweight query) — don't load all profiles
            all_count = storage.client.table("profiles").select("id", count="exact").eq("dataset_id", dataset_id).eq("account_id", aid).execute()
            total = all_count.count or 0

            # Load ONLY pending profiles (not the full dataset)
            pending_resp = (
                storage.client.table("profiles")
                .select("*")
                .eq("dataset_id", dataset_id)
                .eq("account_id", aid)
                .eq("enrichment_status", "pending")
                .limit(CHUNK_SIZE)
                .execute()
            )
            pending = [storage._row_to_profile(r) for r in pending_resp.data]
        except Exception as e:
            json_response(self, 404, {"error": f"Dataset not found: {e}"})
            return

        # Create or resume job
        if not job_id:
            job_id = storage.create_job(dataset_id, total)

        # All profiles processed — do final steps
        if not pending:
            try:
                # Load full dataset for final processing (only runs once)
                dataset = storage.load_dataset(dataset_id)
                # Build profile cards (skip link fetching to reduce I/O)
                pipeline.build_profile_cards(dataset)
                storage.save_profiles(dataset_id, dataset.profiles)
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

        # pending is already limited to CHUNK_SIZE from the query
        chunk = pending

        try:
            # Resolve identities (email → LinkedIn URL) — 20 parallel workers
            pipeline.resolver.resolve_batch(chunk, max_workers=20)

            # Enrich LinkedIn profiles — 20 parallel workers
            pipeline.enricher.enrich_batch(chunk, max_workers=20)

            # Trim enrichment logs before saving (reduces I/O)
            for p in chunk:
                if len(p.enrichment_log) > 20:
                    p.enrichment_log = p.enrichment_log[:5] + ["... truncated ..."] + p.enrichment_log[-5:]

            # Save only the chunk we just processed
            storage.save_profiles(dataset_id, chunk)

            # Count done via lightweight query (not loading all profiles)
            still_pending = storage.client.table("profiles").select("id", count="exact").eq("dataset_id", dataset_id).eq("enrichment_status", "pending").execute()
            done_count = total - (still_pending.count or 0)
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
