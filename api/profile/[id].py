"""GET /api/profile/:id — Get a single profile by ID (searches all datasets)."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        profile_id = path_param(self)
        storage = get_storage(account["account_id"])

        for ds_info in storage.list_datasets():
            ds = storage.load_dataset(ds_info["id"])
            for p in ds.profiles:
                if p.id == profile_id:
                    d = p.to_dict()
                    d["_dataset_name"] = ds_info["name"]
                    d["_dataset_id"] = ds_info["id"]
                    json_response(self, 200, d)
                    return

        json_response(self, 404, {"error": "Profile not found"})

    def log_message(self, format, *args):
        pass
