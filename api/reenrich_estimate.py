"""POST /api/reenrich_estimate — Estimate cost for re-enriching a dataset."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, get_storage
from enrichment.costs import CostBreakdown
from enrichment.enrichers import is_valid_linkedin_url


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

        storage = get_storage(account["account_id"])
        try:
            dataset = storage.load_dataset(dataset_id)
        except Exception:
            json_response(self, 404, {"error": "Dataset not found"})
            return

        profiles = dataset.profiles
        have_li = sum(1 for p in profiles if is_valid_linkedin_url(p.linkedin_url))
        have_email = sum(1 for p in profiles if p.email and not is_valid_linkedin_url(p.linkedin_url))
        no_signal = sum(1 for p in profiles if not p.email and not is_valid_linkedin_url(p.linkedin_url))

        cost = CostBreakdown(
            linkedin_enrichments=len(profiles),
            identity_lookups=len(profiles),
            embedding_profiles=len(profiles),
        )
        cfields = set()
        for p in profiles:
            cfields.update(p.content_fields.keys())

        json_response(self, 200, {
            "stats": {
                "total": len(profiles),
                "have_linkedin": have_li,
                "email_only": have_email,
                "no_signal": no_signal,
                "content_fields": len(cfields),
                "content_field_names": sorted(cfields),
            },
            "cost": cost.to_dict(),
            "cost_summary": cost.summary(),
        })

    def log_message(self, format, *args):
        pass
