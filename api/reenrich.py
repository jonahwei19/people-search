"""POST /api/reenrich — Reset and re-enrich a dataset (chunked)."""

from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, read_json_body,
    get_pipeline, get_storage,
)
from enrichment.models import EnrichmentStatus

CHUNK_SIZE = 10


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        dataset_id = body.get("dataset_id")
        job_id = body.get("job_id")
        reset = body.get("reset", False)

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

        # First call: reset all profiles and create job
        if not job_id or reset:
            for p in dataset.profiles:
                p.enrichment_status = EnrichmentStatus.PENDING
                p.linkedin_enriched = {}
                p.profile_card = ""
                p.field_summaries = {}
                p.fetched_content = {}
            storage.save_dataset(dataset)
            job_id = storage.create_job(dataset_id, total)

        pending = [p for p in dataset.profiles if p.enrichment_status == EnrichmentStatus.PENDING]

        if not pending:
            pipeline.fetch_links(dataset)
            pipeline.build_profile_cards(dataset)
            storage.save_dataset(dataset)
            storage.update_job(job_id, status="done", current_count=total)
            json_response(self, 200, {"job_id": job_id, "status": "done",
                                      "current": total, "total": total})
            return

        chunk = pending[:CHUNK_SIZE]
        try:
            pipeline.resolver.resolve_batch(chunk)
            pipeline.enricher.enrich_batch(chunk)
            storage.save_profiles(dataset.id, dataset.profiles)

            done_count = sum(1 for p in dataset.profiles
                             if p.enrichment_status != EnrichmentStatus.PENDING)
            storage.update_job(job_id, current_count=done_count,
                               message=f"Re-enriched {done_count}/{total}")

            json_response(self, 200, {"job_id": job_id, "status": "running",
                                      "current": done_count, "total": total})
        except Exception as e:
            storage.update_job(job_id, status="error", message=str(e))
            json_response(self, 200, {"job_id": job_id, "status": "error",
                                      "message": str(e),
                                      "current": total - len(pending), "total": total})

    def log_message(self, format, *args):
        pass
