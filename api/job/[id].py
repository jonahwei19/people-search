"""GET/POST /api/job/:id — Job status (GET) or cancel (POST)."""

from http.server import BaseHTTPRequestHandler

from api._helpers import require_auth, json_response, path_param, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return

        job_id = path_param(self)
        storage = get_storage(account["account_id"])
        job = storage.get_job(job_id)

        if not job:
            json_response(self, 404, {"error": "Job not found"})
            return

        json_response(self, 200, job)

    def do_POST(self):
        """Cancel a running job."""
        account = require_auth(self)
        if not account:
            return

        job_id = path_param(self)
        storage = get_storage(account["account_id"])
        job = storage.get_job(job_id)

        if not job:
            json_response(self, 404, {"error": "Job not found"})
            return

        storage.update_job(job_id, status="cancelled", message="Cancelled by user")
        json_response(self, 200, {"ok": True})

    def log_message(self, format, *args):
        pass
