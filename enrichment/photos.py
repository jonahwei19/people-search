"""Profile photo cache.

LinkedIn CDN URLs expire (~30 days), so we download the photo once and
upload it to a Supabase Storage bucket (`facebook-photos`). The face-book
gallery and any future profile-card UI reads from there.

The bucket is public; LinkedIn profile photos are public anyway, and the
gallery URL is gated by per-account auth on the API endpoint that
generates it. If you ever need stricter access, switch to signed URLs in
`public_url()` below.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

BUCKET = "facebook-photos"
_USER_AGENT = "Mozilla/5.0 (people-search face-book builder)"
_REQUEST_TIMEOUT = 15


def _ext_for(url: str, content_type: str = "") -> str:
    u = (url or "").lower()
    if ".png" in u or "image/png" in content_type.lower():
        return "png"
    if ".webp" in u or "image/webp" in content_type.lower():
        return "webp"
    return "jpg"


def public_url(supabase_url: str, path: str) -> str:
    """Return the public URL for a stored photo. `path` is what cache_photo returned."""
    base = supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{BUCKET}/{path}"


def cache_photo(
    supabase_client,
    profile_id: str,
    photo_url: str,
    *,
    overwrite: bool = False,
) -> Optional[str]:
    """Download `photo_url` and upload to Supabase Storage. Return the storage path.

    Returns the path within the bucket (e.g. `<profile_id>.jpg`) on success,
    None on any failure. Failures are silent — photo caching must NEVER block
    enrichment or search; the face-book just shows initials for that profile.
    """
    if not photo_url or not profile_id:
        return None
    try:
        resp = requests.get(
            photo_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200 or not resp.content:
            return None
        ext = _ext_for(photo_url, resp.headers.get("content-type", ""))
        path = f"{profile_id}.{ext}"
        # supabase-py storage upload — `upsert=true` so re-runs are idempotent.
        file_options = {
            "content-type": resp.headers.get("content-type", f"image/{ext}"),
            "cache-control": "public, max-age=31536000, immutable",
            "upsert": "true" if overwrite else "false",
        }
        try:
            supabase_client.storage.from_(BUCKET).upload(
                path=path,
                file=resp.content,
                file_options=file_options,
            )
        except Exception as upload_err:
            # If the file already exists and overwrite=False, treat as success
            # — the cached photo is what we want.
            msg = str(upload_err).lower()
            if "duplicate" in msg or "already exists" in msg or "409" in msg:
                return path
            return None
        return path
    except requests.RequestException:
        return None
    except Exception:
        return None


def needs_caching(profile) -> Optional[str]:
    """Return the photo URL to fetch if the profile needs photo caching.

    Returns None when the profile already has a cached photo OR has no source
    URL to fetch.
    """
    if getattr(profile, "photo_path", "") or "":
        return None
    enriched = getattr(profile, "linkedin_enriched", None) or {}
    if not isinstance(enriched, dict):
        return None
    url = enriched.get("profile_pic_url") or ""
    return url or None
