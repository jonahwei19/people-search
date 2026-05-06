"""GET /api/search/searches — List saved searches.

Query params:
    include_archived=1  — also return archived searches (default: hidden)
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from api._helpers import require_auth, json_response, get_storage


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if not account:
            return
        qs = parse_qs(urlparse(self.path).query)
        include_archived = qs.get("include_archived", ["0"])[0] in ("1", "true", "yes")
        storage = get_storage(account["account_id"])
        json_response(self, 200, {
            "searches": storage.list_searches(include_archived=include_archived),
        })

    def log_message(self, format, *args):
        pass
