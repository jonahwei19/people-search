"""POST /api/dataset/:id/build_photos — Cache LinkedIn profile photos.

Walks every profile in the dataset, downloads `linkedin_enriched.profile_pic_url`
into the `facebook-photos` Supabase Storage bucket, and stores the resulting
path on `profiles.photo_path`. Idempotent: profiles that already have a
photo_path are skipped.

Body (optional): { "force": true } to re-download even if photo_path is set.

Returns: { "cached": N, "skipped": N, "no_url": N, "failed": N, "total": N }

Note: this is the on-demand backfill path. New enrichments cache photos
eagerly via enrichment.photos.cache_photo() called from the pipeline.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage
from enrichment.photos import cache_photo


# Cap per request so we stay well under Vercel's 300s budget. For larger
# datasets, the UI re-issues the request until total == cached + skipped + ...
_BATCH_CAP = 200


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        ds_id = path_param(self, -2)  # /api/dataset/<id>/build_photos
        body = read_json_body(self)
        force = bool(body.get("force"))

        storage = get_storage(account["account_id"])
        try:
            ds = storage.load_dataset(ds_id)
        except Exception:
            json_response(self, 404, {"error": "Dataset not found"})
            return

        stats = {"cached": 0, "skipped": 0, "no_url": 0, "failed": 0, "total": len(ds.profiles)}
        worked = 0

        for profile in ds.profiles:
            if worked >= _BATCH_CAP:
                stats["batch_capped"] = True
                break
            if profile.photo_path and not force:
                stats["skipped"] += 1
                continue
            url = ""
            enriched = profile.linkedin_enriched if isinstance(profile.linkedin_enriched, dict) else {}
            url = enriched.get("profile_pic_url") or ""
            if not url:
                stats["no_url"] += 1
                continue

            path = cache_photo(storage.client, profile.id, url, overwrite=force)
            worked += 1
            if not path:
                stats["failed"] += 1
                continue

            profile.photo_path = path
            try:
                # Persist only the photo_path column to avoid re-writing the
                # large JSONB blobs.
                storage.client.table("profiles").update(
                    {"photo_path": path}
                ).eq("id", profile.id).eq("account_id", account["account_id"]).execute()
                stats["cached"] += 1
            except Exception:
                stats["failed"] += 1

        json_response(self, 200, stats)

    def log_message(self, format, *args):
        pass
