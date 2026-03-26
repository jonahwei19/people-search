"""POST /api/auth_login — Authenticate with account name + password.

Request body: {"name": "BlueDot", "password": "secret"}

Success (200): {"ok": true, "account": {"id": "...", "name": "..."}}
  + Set-Cookie: ps_session=<signed_token>; HttpOnly; Secure; SameSite=Lax

Failure (401): {"error": "Invalid account name or password"}
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud.auth import (
    get_supabase_client,
    verify_login,
    create_session_token,
    make_session_cookie,
    json_response,
    read_json_body,
)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = read_json_body(self)

        name = (body.get("name") or "").strip()
        password = body.get("password") or ""

        if not name or not password:
            json_response(self, 400, {"error": "Name and password are required"})
            return

        client = get_supabase_client()
        account = verify_login(client, name, password)

        if account is None:
            json_response(self, 401, {"error": "Invalid account name or password"})
            return

        # Create signed session token and set cookie
        token = create_session_token(account["id"], account["name"])
        secure = os.environ.get("VERCEL_ENV") != "development"
        cookie = make_session_cookie(token, secure=secure)

        data = {
            "ok": True,
            "account": {"id": account["id"], "name": account["name"]},
        }

        import json as _json

        response_bytes = _json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        """Suppress default stderr logging in serverless."""
        pass
