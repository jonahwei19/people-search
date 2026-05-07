"""POST /api/profile/:id/photo — Manual photo override.

Body shapes:
  { "photo_url": "https://..." }   → download from that URL, cache, set photo_path
  { "clear": true }                → drop the cached photo (face-book shows initials)

This is the manual escape hatch when neither EnrichLayer nor Gravatar
gives a usable photo for someone. Paste any public image URL (org
website headshot, GitHub avatar, Twitter pic, etc.) and we cache it
into facebook-photos the same way.
"""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, read_json_body, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        account = require_auth(self)
        if not account:
            return

        profile_id = path_param(self, -2)  # .../profile/<id>/photo
        body = read_json_body(self)
        photo_url = (body.get("photo_url") or "").strip()
        clear = bool(body.get("clear"))

        if not photo_url and not clear:
            json_response(self, 400, {"error": "Provide photo_url or clear=true"})
            return

        from enrichment.photos import cache_photo, delete_photo

        storage = get_storage(account["account_id"])
        # Find the profile across the account's datasets. Same scan pattern as
        # /api/profile/<id>/linkedin — fine here because there's no scoping
        # requirement and load_profile would need a dataset_id we don't have.
        target = None
        target_ds = None
        for ds_info in storage.list_datasets():
            ds = storage.load_dataset(ds_info["id"])
            for p in ds.profiles:
                if p.id == profile_id:
                    target, target_ds = p, ds_info["id"]
                    break
            if target:
                break

        if not target:
            json_response(self, 404, {"error": "Profile not found"})
            return

        old_photo = getattr(target, "photo_path", "") or ""

        if clear:
            if old_photo:
                delete_photo(storage.client, old_photo)
            storage.client.table("profiles").update(
                {"photo_path": ""}
            ).eq("id", target.id).eq("account_id", account["account_id"]).execute()
            json_response(self, 200, {"status": "cleared"})
            return

        # Validate the URL has a scheme. Allow http(s) only.
        if not (photo_url.startswith("http://") or photo_url.startswith("https://")):
            json_response(self, 400, {"error": "photo_url must start with http:// or https://"})
            return

        # Drop the old file before re-caching so it's a clean overwrite.
        if old_photo:
            delete_photo(storage.client, old_photo)

        path, reason = cache_photo(storage.client, target.id, photo_url, overwrite=True)
        if not path:
            json_response(self, 502, {"error": f"Could not fetch image: {reason}"})
            return

        storage.client.table("profiles").update(
            {"photo_path": path}
        ).eq("id", target.id).eq("account_id", account["account_id"]).execute()

        json_response(self, 200, {"status": "cached", "photo_path": path})

    def log_message(self, format, *args):
        pass
