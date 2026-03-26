"""POST /api/embed_only — Skip enrichment, just build profile cards."""

from http.server import BaseHTTPRequestHandler

from api._helpers import (
    require_auth, json_response, read_json_body,
    get_pipeline, get_storage,
)
from enrichment.models import EnrichmentStatus


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        body = read_json_body(self)
        dataset_id = body.get("dataset_id")
        if not dataset_id:
            json_response(self, 400, {"error": "dataset_id required"})
            return

        aid = account["account_id"]
        storage = get_storage(aid)
        pipeline = get_pipeline(aid)

        try:
            dataset = storage.load_dataset(dataset_id)

            # Mark pending profiles as skipped
            for p in dataset.profiles:
                if p.enrichment_status == EnrichmentStatus.PENDING:
                    p.enrichment_status = EnrichmentStatus.SKIPPED

            # Fetch links if available
            pipeline.fetch_links(dataset)

            # Build profile cards from whatever content exists
            pipeline.build_profile_cards(dataset)
            storage.save_dataset(dataset)

            job_id = storage.create_job(dataset_id, len(dataset.profiles))
            storage.update_job(job_id, status="done", current_count=len(dataset.profiles),
                               stats={"enriched": 0, "skipped": len(dataset.profiles),
                                       "failed": 0, "total_cost": 0})

            json_response(self, 200, {"job_id": job_id, "status": "done"})
        except Exception as e:
            json_response(self, 400, {"error": str(e)})

    def log_message(self, format, *args):
        pass
