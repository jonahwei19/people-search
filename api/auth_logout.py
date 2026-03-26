"""POST /api/auth_logout — Clear the session cookie.

No request body needed.

Response (200): {"ok": true}
  + Set-Cookie: ps_session=; Max-Age=0  (clears cookie)
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud.auth import make_clear_cookie


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        cookie = make_clear_cookie()
        data = json.dumps({"ok": True}).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass
