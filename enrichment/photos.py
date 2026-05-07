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

import hashlib
import html as _html
import os
import re
import time
from typing import Optional, Tuple

import requests

# LinkedIn renders its public profile HTML to social-card crawlers
# (Facebook, Slack, Twitter) with the profile photo URL signed and
# usable. We use the FB external-hit UA which is the most reliably
# served. The signed URL has a `?e=…&v=beta&t=…` token; without it
# media.licdn.com returns 403.
_LI_BOT_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_LI_PHOTO_RE = re.compile(
    r'(https://media\.licdn\.com/dms/image/[^"\\<>\s]+profile-displayphoto[^"\\<>\s]*\?e=\d+[^"\\<>\s]+t=[A-Za-z0-9_\-]+)'
)


def scrape_linkedin_photo_url(linkedin_url: str) -> str:
    """Fetch a LinkedIn /in/ page as a social-card bot and extract the
    first signed profile-displayphoto URL. Returns "" on failure.

    This bypasses the gap where EnrichLayer's `profile_pic_url` is null
    for some profiles (private / connections-only / scraper miss) but
    LinkedIn still serves the photo URL inline to crawlers like Facebook.
    """
    if not linkedin_url:
        return ""
    s = linkedin_url.strip()
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    try:
        resp = requests.get(
            s,
            headers={"User-Agent": _LI_BOT_UA},
            timeout=15,
            allow_redirects=True,
        )
    except requests.RequestException:
        return ""
    if resp.status_code != 200 or not resp.text:
        return ""
    # &amp; entities have to be decoded before the regex sees them.
    text = _html.unescape(resp.text)
    m = _LI_PHOTO_RE.search(text)
    return m.group(1) if m else ""


def refresh_photo(
    supabase_client,
    profile,
    account_id: str,
    *,
    enricher=None,
    overwrite: bool = True,
) -> Tuple[Optional[str], str]:
    """Run the full photo-cache fallback chain for one profile and persist.

    Tries, in order:
      1. cached `linkedin_enriched.profile_pic_url`
      2. EnrichLayer fresh fetch
      3. LinkedIn HTML scrape (signed photo URL via crawler UA)
      4. Gravatar (md5 of email, d=404)

    On success, updates `profiles.photo_path` in the DB and returns
    (path, ""). On failure, returns (None, reason). Used by both the
    on-demand backfill (api/dataset/[id]/build_photos.py) and by the
    LinkedIn-correction endpoint (api/profile/[id]/linkedin.py) so a
    new URL re-caches the photo automatically.
    """
    # Step 1: try whatever URL we already have.
    enriched = profile.linkedin_enriched if isinstance(profile.linkedin_enriched, dict) else {}
    url = enriched.get("profile_pic_url") or ""

    # Step 2: EnrichLayer fresh.
    if not url and getattr(profile, "linkedin_url", ""):
        if enricher is None:
            from .enrichers import LinkedInEnricher
            enricher = LinkedInEnricher()
        if enricher.api_key:
            try:
                from .enrichers import normalize_linkedin_url
                raw, _ = enricher._call_api(normalize_linkedin_url(profile.linkedin_url))
                if isinstance(raw, dict):
                    url = (raw.get("profile_pic_url") or "").strip()
                    if url:
                        enriched["profile_pic_url"] = url
                        profile.linkedin_enriched = enriched
                        try:
                            supabase_client.table("profiles").update(
                                {"linkedin_enriched": enriched}
                            ).eq("id", profile.id).eq("account_id", account_id).execute()
                        except Exception:
                            pass
            except Exception:
                pass

    # NOTE: previously had a step 3 that scraped LinkedIn's HTML directly
    # for a signed profile-displayphoto URL. Disabled — the first match in
    # the HTML is often a sidebar profile, not the page subject, leading to
    # wrong photos. EnrichLayer is the authoritative source.

    # Step 3: Gravatar.
    if not url and getattr(profile, "email", ""):
        url = gravatar_url(profile.email)

    if not url:
        return None, "no_source"

    path, reason = cache_photo(supabase_client, profile.id, url, overwrite=overwrite)
    if not path:
        return None, reason or "cache_failed"

    profile.photo_path = path
    try:
        supabase_client.table("profiles").update(
            {"photo_path": path}
        ).eq("id", profile.id).eq("account_id", account_id).execute()
    except Exception as e:
        return None, f"db_update:{type(e).__name__}"
    return path, ""


def gravatar_url(email: str) -> str:
    """Return a Gravatar URL for the email, or "" if email is blank.

    `d=404` means: if the user hasn't registered a Gravatar for this
    email, return a 404 instead of a default cartoon image — so our
    download logic correctly treats it as "no photo" and falls through
    to initials.
    """
    if not email or not isinstance(email, str):
        return ""
    h = hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()
    return f"https://www.gravatar.com/avatar/{h}?s=400&d=404"

BUCKET = "facebook-photos"
# LinkedIn CDN sometimes 403s the default Python UA; a real-browser UA
# is more reliable. EnrichLayer's CDN doesn't care.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
_REQUEST_TIMEOUT = 15
# Reasons that mean "URL is dead, re-fetching from EnrichLayer would
# probably get a different one." Caller uses these to decide retries.
URL_DEAD_REASONS = {"download_403", "download_404", "download_410"}


def _ext_for(url: str, content_type: str = "") -> str:
    u = (url or "").lower()
    if ".png" in u or "image/png" in content_type.lower():
        return "png"
    if ".webp" in u or "image/webp" in content_type.lower():
        return "webp"
    return "jpg"


def delete_photo(supabase_client, photo_path: str) -> bool:
    """Remove a cached photo from the bucket. Silent on failure."""
    if not photo_path:
        return True
    try:
        supabase_client.storage.from_(BUCKET).remove([photo_path])
        return True
    except Exception:
        return False


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
) -> Tuple[Optional[str], str]:
    """Download `photo_url` and upload to Supabase Storage.

    Returns (path, reason). On success, path is the bucket-relative path
    (e.g. `<profile_id>.jpg`) and reason is "". On failure, path is None
    and reason is a short tag like "download_404", "download_timeout",
    "upload_error", etc. — surfaced in build_photos stats so we can see
    what's actually breaking.

    Retries once on transient errors (timeout, 5xx, 429, connection error)
    before giving up. Permanent failures (4xx other than 429) are not
    retried — caller should refresh the URL via EnrichLayer and try again.
    """
    if not photo_url:
        return None, "no_url"
    if not profile_id:
        return None, "no_profile_id"

    content: Optional[bytes] = None
    content_type = ""
    last_status: Optional[int] = None
    last_error = ""

    for attempt in range(2):
        try:
            resp = requests.get(
                photo_url,
                headers={"User-Agent": _USER_AGENT, "Accept": "image/*"},
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            last_status = resp.status_code
            if resp.status_code == 200 and resp.content and len(resp.content) >= 256:
                content = resp.content
                content_type = resp.headers.get("content-type", "") or ""
                break
            # Retry on rate limit + transient server errors.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(0.6)
                continue
            return None, f"download_{resp.status_code}"
        except requests.Timeout:
            last_error = "timeout"
            if attempt == 0:
                continue
            return None, "download_timeout"
        except requests.RequestException as e:
            last_error = type(e).__name__.lower()
            if attempt == 0:
                time.sleep(0.4)
                continue
            return None, f"download_error:{last_error}"

    if content is None:
        return None, f"download_{last_status or last_error or 'unknown'}"

    ext = _ext_for(photo_url, content_type)
    path = f"{profile_id}.{ext}"
    file_options = {
        "content-type": content_type or f"image/{ext}",
        "cache-control": "public, max-age=31536000, immutable",
        "upsert": "true" if overwrite else "false",
    }
    try:
        supabase_client.storage.from_(BUCKET).upload(
            path=path,
            file=content,
            file_options=file_options,
        )
    except Exception as upload_err:
        msg = str(upload_err).lower()
        # Treat "already there" as success — re-runs should be no-ops.
        if "duplicate" in msg or "already exists" in msg or "409" in msg:
            return path, ""
        return None, f"upload_error:{msg[:60]}"
    return path, ""


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
