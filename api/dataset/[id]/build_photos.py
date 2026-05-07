"""POST /api/dataset/:id/build_photos — Cache LinkedIn profile photos.

Walks every profile in the dataset and caches photos into the
`facebook-photos` Supabase Storage bucket. Order of operations per profile:
  1. If photo_path is already set (and !force), skip.
  2. If linkedin_enriched.profile_pic_url is set, download it.
  3. Else if profile.linkedin_url is set, call EnrichLayer to fetch the
     photo URL (and update linkedin_enriched in place), then download.

Idempotent across requests. The frontend loops on `batch_capped` until the
whole dataset is processed, since each request runs at most _BATCH_CAP
profiles to stay under the Vercel 300s budget.

Body (optional): { "force": true } to re-download even if photo_path is set.

Returns: { "cached": N, "skipped": N, "no_url": N, "failed": N,
           "total": N, "batch_capped": bool }
"""

from __future__ import annotations

import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage
from enrichment.photos import cache_photo
from enrichment.enrichers import LinkedInEnricher, normalize_linkedin_url


# Cap per request so we stay well under Vercel's 300s budget. EnrichLayer
# fallback adds ~1s/profile; the cache-only path is sub-second. The UI
# loops on batch_capped, so a too-small cap just means more round trips.
_BATCH_CAP = 100


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

        stats = {
            "cached": 0,
            "skipped": 0,
            "no_url": 0,
            "failed": 0,
            "enriched": 0,        # profiles where we re-fetched photo URL via EnrichLayer
            "total": len(ds.profiles),
            "batch_capped": False,
        }
        worked = 0
        enricher = None  # lazily init only if we actually need it

        for profile in ds.profiles:
            if worked >= _BATCH_CAP:
                stats["batch_capped"] = True
                break
            if profile.photo_path and not force:
                stats["skipped"] += 1
                continue

            enriched = profile.linkedin_enriched if isinstance(profile.linkedin_enriched, dict) else {}
            url = enriched.get("profile_pic_url") or ""

            # Fall back to EnrichLayer when we have a LinkedIn URL but no
            # cached photo URL (typically: profiles enriched before the
            # _parse_response patch that started keeping profile_pic_url).
            if not url and profile.linkedin_url:
                if enricher is None:
                    enricher = LinkedInEnricher()
                if not enricher.api_key:
                    stats["no_url"] += 1
                    continue
                try:
                    raw, _reason = enricher._call_api(normalize_linkedin_url(profile.linkedin_url))
                except Exception:
                    raw = None
                if raw and raw != "OUT_OF_CREDITS" and isinstance(raw, dict):
                    url = (raw.get("profile_pic_url") or "").strip()
                    if url:
                        # Persist the URL into linkedin_enriched so we never
                        # have to re-fetch it for this profile again.
                        enriched["profile_pic_url"] = url
                        profile.linkedin_enriched = enriched
                        try:
                            storage.client.table("profiles").update(
                                {"linkedin_enriched": enriched}
                            ).eq("id", profile.id).eq(
                                "account_id", account["account_id"]
                            ).execute()
                        except Exception:
                            pass  # photo download still useful even if save failed
                        stats["enriched"] += 1
                # Be polite to EnrichLayer regardless of outcome.
                time.sleep(0.15)

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
