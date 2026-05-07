"""POST /api/profile/:id/linkedin — Update LinkedIn URL + re-enrich + re-cache photo.

Body: { "linkedin_url": "<url>" } to set, or { "linkedin_url": "" } to clear.

When set: calls EnrichLayer for the new URL, replaces linkedin_enriched,
and runs the photo-cache fallback chain so the face-book gallery picks
up the new photo immediately.

When cleared: drops the linkedin URL, the enriched data, the cached
photo path, and removes the photo file from the storage bucket.
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
        from enrichment.photos import delete_photo, refresh_photo

        for ds_info in storage.list_datasets():
            ds = storage.load_dataset(ds_info["id"])
            for p in ds.profiles:
                if p.id != profile_id:
                    continue

                if not new_url:
                    # Clear LinkedIn + drop the cached photo entirely.
                    old_photo = getattr(p, "photo_path", "") or ""
                    p.linkedin_url = ""
                    p.linkedin_enriched = {}
                    p.enrichment_status = EnrichmentStatus.FAILED
                    p.enrichment_log.append("LinkedIn manually cleared by user")
                    p.profile_card = ""
                    p.photo_path = ""
                    p.build_raw_text()
                    storage.save_profiles(ds_info["id"], [p])
                    # save_profiles' row-mapper conditionally writes photo_path
                    # only when truthy (for pre-migration schemas), so an
                    # explicit UPDATE is needed to clear it. Same for
                    # linkedin_enriched — cleared via the upsert above.
                    storage.client.table("profiles").update(
                        {"photo_path": ""}
                    ).eq("id", p.id).eq("account_id", account["account_id"]).execute()
                    if old_photo:
                        delete_photo(storage.client, old_photo)
                    json_response(self, 200, {"status": "cleared"})
                    return

                # Set a corrected LinkedIn URL → re-enrich + re-cache photo.
                old_photo = getattr(p, "photo_path", "") or ""
                p.linkedin_url = new_url
                p.enrichment_log.append(f"LinkedIn manually set to: {new_url}")

                from enrichment.enrichers import normalize_linkedin_url
                url = normalize_linkedin_url(new_url)
                api_data, _reason = pipeline.enricher._call_api(url)

                if api_data and api_data != "OUT_OF_CREDITS":
                    parsed = pipeline.enricher._parse_response(api_data)
                    p.linkedin_enriched = parsed
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    p.enrichment_log.append(
                        f"LinkedIn enriched (manual): {len(parsed.get('experience', []))} experiences"
                    )
                    if not p.name and parsed.get("full_name"):
                        p.name = parsed["full_name"]
                else:
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    p.enrichment_log.append("LinkedIn set but no data from EnrichLayer")

                # Drop the old (wrong) photo from the bucket before recaching.
                if old_photo:
                    delete_photo(storage.client, old_photo)
                p.photo_path = ""
                p.build_raw_text()
                storage.save_profiles(ds_info["id"], [p])
                # Explicit clear: the row-mapper in save_profiles skips
                # photo_path when it's empty, so we'd otherwise leave the
                # stale (now-bucket-orphaned) path in DB if refresh_photo
                # below fails to find a new source.
                storage.client.table("profiles").update(
                    {"photo_path": ""}
                ).eq("id", p.id).eq("account_id", account["account_id"]).execute()

                # Re-cache the photo through the full fallback chain. Pulls
                # from linkedin_enriched.profile_pic_url first; falls through
                # to a fresh EnrichLayer call (in case _call_api skipped it),
                # then Gravatar.
                try:
                    refresh_photo(
                        storage.client, p, account["account_id"],
                        enricher=pipeline.enricher, overwrite=True,
                    )
                except Exception:
                    pass  # photo recache is best-effort; never block the response

                json_response(self, 200, {"status": "enriched", "profile": p.to_dict()})
                return

        json_response(self, 404, {"error": "Profile not found"})

    def log_message(self, format, *args):
        pass
