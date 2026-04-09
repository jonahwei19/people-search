"""GET/POST /api/keys — Per-account API key management.

GET  → {"keys": {"BRAVE_API_KEY": "...", "SERPER_API_KEY": "...", ...}}
POST → body: {"BRAVE_API_KEY": "new_value", ...}
       response: {"ok": true}

Requires auth (ps_session cookie).
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud.auth import (
    get_supabase_client,
    get_account_keys,
    update_account_keys,
    json_response,
    read_json_body,
    require_auth,
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if account is None:
            return

        client = get_supabase_client()
        keys = get_account_keys(client, account["account_id"])
        json_response(self, 200, {"keys": keys, "account_name": account.get("name", "")})

    def do_POST(self):
        account = require_auth(self)
        if account is None:
            return

        body = read_json_body(self)
        if not body:
            json_response(self, 400, {"error": "No keys provided"})
            return

        client = get_supabase_client()
        update_account_keys(client, account["account_id"], body)
        json_response(self, 200, {"ok": True})

    def log_message(self, format, *args):
        pass
