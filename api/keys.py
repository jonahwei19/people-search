"""GET/POST /api/keys — Per-account API key management.

GET  → {"keys": {"BRAVE_API_KEY": "...", ...}, "platform_defaults": ["BRAVE_API_KEY", ...]}
POST → body: {"BRAVE_API_KEY": "new_value", ...}
       response: {"ok": true}

Requires auth (ps_session cookie).
"""

from __future__ import annotations

import os
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

ALL_KEYS = ["BRAVE_API_KEY", "SERPER_API_KEY", "ENRICHLAYER_API_KEY", "GOOGLE_API_KEY"]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        account = require_auth(self)
        if account is None:
            return

        client = get_supabase_client()
        keys = get_account_keys(client, account["account_id"])
        # Tell the frontend which keys have platform defaults (without exposing values)
        platform_defaults = [k for k in ALL_KEYS if not keys.get(k) and os.environ.get(k)]
        json_response(self, 200, {
            "keys": keys,
            "account_name": account.get("name", ""),
            "platform_defaults": platform_defaults,
        })

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
