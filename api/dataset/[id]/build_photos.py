"""POST /api/dataset/:id/build_photos — Cache LinkedIn profile photos.

Walks every profile in the dataset and caches photos into the
`facebook-photos` Supabase Storage bucket. Per profile:

  1. If photo_path is already set (and !force), skip.
  2. Try to download linkedin_enriched.profile_pic_url.
  3. If that URL is missing OR returns 404/403/410 (CDN URLs expire),
     re-fetch from EnrichLayer for a fresh URL and try once more.
  4. On transient errors (timeouts, 5xx), cache_photo retries once
     internally; if still failing, mark as failed and move on — next
     click is idempotent and will retry.

Idempotent across requests. The frontend loops on `batch_capped` until
the whole dataset is processed, since each request runs at most
_BATCH_CAP profiles to stay under the Vercel 300s budget.

Body (optional): { "force": true } to re-download even if photo_path is set.

Returns: { cached, skipped, no_url, failed, enriched, total,
           batch_capped, fail_reasons: {reason: count} }
"""

from __future__ import annotations

import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage
from enrichment.photos import cache_photo, gravatar_url, URL_DEAD_REASONS
from enrichment.enrichers import LinkedInEnricher, normalize_linkedin_url


# Cap per request so we stay well under Vercel's 300s budget. EnrichLayer
# fallback adds ~1s/profile; the cache-only path is sub-second. The UI
# loops on batch_capped, so a too-small cap just means more round trips.
_BATCH_CAP = 100


def _fresh_url_from_enrichlayer(enricher, linkedin_url):
    """Call EnrichLayer for the latest profile_pic_url. Returns "" on failure."""
    if not linkedin_url or not enricher or not enricher.api_key:
        return ""
    try:
        raw, _reason = enricher._call_api(normalize_linkedin_url(linkedin_url))
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    return (raw.get("profile_pic_url") or "").strip()


def _persist_enriched_url(storage, account_id, profile, url):
    """Save the refreshed profile_pic_url into linkedin_enriched."""
    enriched = profile.linkedin_enriched if isinstance(profile.linkedin_enriched, dict) else {}
    enriched["profile_pic_url"] = url
    profile.linkedin_enriched = enriched
    try:
        storage.client.table("profiles").update(
            {"linkedin_enriched": enriched}
        ).eq("id", profile.id).eq("account_id", account_id).execute()
    except Exception:
        pass  # photo download still useful even if save failed


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
            "fail_reasons": {},
        }
        worked = 0
        enricher = None  # lazily init only if we actually need it

        def _bump_reason(reason):
            stats["fail_reasons"][reason] = stats["fail_reasons"].get(reason, 0) + 1

        for profile in ds.profiles:
            if worked >= _BATCH_CAP:
                stats["batch_capped"] = True
                break
            if profile.photo_path and not force:
                stats["skipped"] += 1
                continue

            enriched = profile.linkedin_enriched if isinstance(profile.linkedin_enriched, dict) else {}
            url = enriched.get("profile_pic_url") or ""

            # No cached URL → ask EnrichLayer (typically: profiles enriched
            # before the _parse_response patch that kept profile_pic_url).
            if not url and profile.linkedin_url:
                if enricher is None:
                    enricher = LinkedInEnricher()
                url = _fresh_url_from_enrichlayer(enricher, profile.linkedin_url)
                if url:
                    _persist_enriched_url(storage, account["account_id"], profile, url)
                    stats["enriched"] += 1
                time.sleep(0.15)

            # If still no URL after EnrichLayer, try Gravatar before giving up.
            # This catches profiles where EnrichLayer's scrape returned null
            # for profile_pic_url (private/connections-only/missed) but the
            # person has a Gravatar registered against their email.
            if not url and getattr(profile, "email", ""):
                gv = gravatar_url(profile.email)
                if gv:
                    gv_path, _gv_reason = cache_photo(storage.client, profile.id, gv, overwrite=force)
                    if gv_path:
                        profile.photo_path = gv_path
                        try:
                            storage.client.table("profiles").update(
                                {"photo_path": gv_path}
                            ).eq("id", profile.id).eq("account_id", account["account_id"]).execute()
                            stats["cached"] += 1
                            stats["gravatar"] = stats.get("gravatar", 0) + 1
                        except Exception as e:
                            stats["failed"] += 1
                            _bump_reason(f"db_update:{type(e).__name__}")
                        worked += 1
                        continue

            if not url:
                stats["no_url"] += 1
                continue

            # First attempt with whatever URL we have.
            path, reason = cache_photo(storage.client, profile.id, url, overwrite=force)
            worked += 1

            # If the URL itself is dead, refresh via EnrichLayer and retry
            # once. CDN URLs (LinkedIn licdn, EnrichLayer assets) rotate.
            if not path and reason in URL_DEAD_REASONS and profile.linkedin_url:
                if enricher is None:
                    enricher = LinkedInEnricher()
                fresh = _fresh_url_from_enrichlayer(enricher, profile.linkedin_url)
                if fresh and fresh != url:
                    _persist_enriched_url(storage, account["account_id"], profile, fresh)
                    stats["enriched"] += 1
                    path, reason = cache_photo(storage.client, profile.id, fresh, overwrite=True)
                time.sleep(0.15)

            # Gravatar fallback: when EnrichLayer can't supply a photo
            # (private profile / connections-only / scraper missed it),
            # try the email's Gravatar. d=404 means we only get a real
            # bytestream if the user actually registered one.
            if not path and getattr(profile, "email", ""):
                gv = gravatar_url(profile.email)
                if gv:
                    gv_path, gv_reason = cache_photo(storage.client, profile.id, gv, overwrite=True)
                    if gv_path:
                        path, reason = gv_path, ""
                        stats["gravatar"] = stats.get("gravatar", 0) + 1

            if not path:
                stats["failed"] += 1
                _bump_reason(reason or "unknown")
                continue

            profile.photo_path = path
            try:
                # Persist only the photo_path column to avoid re-writing the
                # large JSONB blobs.
                storage.client.table("profiles").update(
                    {"photo_path": path}
                ).eq("id", profile.id).eq("account_id", account["account_id"]).execute()
                stats["cached"] += 1
            except Exception as e:
                stats["failed"] += 1
                _bump_reason(f"db_update:{type(e).__name__}")

        json_response(self, 200, stats)

    def log_message(self, format, *args):
        pass
