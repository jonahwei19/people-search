"""POST /api/profile/:id/linkedin — Update LinkedIn URL + re-enrich.

Delegates to the parent profile handler's POST logic.
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage, get_pipeline


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        profile_id = path_param(self, -2)  # .../profile/{id}/linkedin
        data = read_json_body(self)
        new_url = (data.get("linkedin_url") or "").strip()
        storage = get_storage(account["account_id"])
        pipeline = get_pipeline(account)

        from enrichment.models import EnrichmentStatus

        for ds_info in storage.list_datasets():
            ds = storage.load_dataset(ds_info["id"])
            for p in ds.profiles:
                if p.id == profile_id:
                    if not new_url:
                        p.linkedin_url = ""
                        p.linkedin_enriched = {}
                        p.enrichment_status = EnrichmentStatus.FAILED
                        p.enrichment_log.append("LinkedIn manually cleared by user")
                        p.profile_card = ""
                        p.build_raw_text()
                        storage.save_profiles(ds_info["id"], [p])
                        json_response(self, 200, {"status": "cleared"})
                        return

                    p.linkedin_url = new_url
                    p.enrichment_log.append(f"LinkedIn manually set to: {new_url}")

                    from enrichment.enrichers import normalize_linkedin_url
                    url = normalize_linkedin_url(new_url)
                    api_data, _reason = pipeline.enricher._call_api(url)

                    if api_data and api_data != "OUT_OF_CREDITS":
                        parsed = pipeline.enricher._parse_response(api_data)
                        p.linkedin_enriched = parsed
                        p.enrichment_status = EnrichmentStatus.ENRICHED
                        p.enrichment_log.append(f"LinkedIn enriched (manual): {len(parsed.get('experience', []))} experiences")
                        if not p.name and parsed.get("full_name"):
                            p.name = parsed["full_name"]
                    else:
                        p.enrichment_status = EnrichmentStatus.ENRICHED
                        p.enrichment_log.append("LinkedIn set but no data from EnrichLayer")

                    p.build_raw_text()
                    storage.save_profiles(ds_info["id"], [p])
                    json_response(self, 200, {"status": "enriched", "profile": p.to_dict()})
                    return

        json_response(self, 404, {"error": "Profile not found"})

    def log_message(self, format, *args):
        pass
