"""Account authentication for People Search Cloud.

Handles:
- Password verification via Supabase's verify_login() RPC
- Signed session cookies (HMAC-SHA256, stdlib only — no extra deps)
- Auth middleware for API routes (require_auth decorator)
- Per-account API key read/write on accounts.settings JSONB

Session cookie: ps_session, HttpOnly, Secure, SameSite=Lax, 7-day expiry.
Token format: base64url(json_payload).base64url(hmac_signature)

Env vars required:
- SUPABASE_URL
- SUPABASE_SERVICE_KEY
- SESSION_SECRET  (any random string, used to sign cookies)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from functools import wraps
from http.cookies import SimpleCookie
from typing import Any

from supabase import create_client, Client

# ── Constants ────────────────────────────────────────────────

COOKIE_NAME = "ps_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 days
SESSION_TTL = COOKIE_MAX_AGE

# Keys we store in accounts.settings
API_KEY_FIELDS = [
    "BRAVE_API_KEY",
    "SERPER_API_KEY",
    "ENRICHLAYER_API_KEY",
    "GOOGLE_API_KEY",
]


# ── Supabase client ──────────────────────────────────────────

def get_supabase_client() -> Client:
    """Create a Supabase client using env vars. Uses the service key (bypasses RLS)."""
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


# ── Password verification ────────────────────────────────────

def verify_login(client: Client, name: str, password: str) -> dict | None:
    """Verify account credentials via the verify_login() SQL function.

    Returns {"id": ..., "name": ..., "settings": ...} on success, None on failure.
    """
    response = client.rpc(
        "verify_login",
        {"p_name": name, "p_password": password},
    ).execute()

    if not response.data:
        return None

    row = response.data[0]
    return {
        "id": row["id"],
        "name": row["name"],
        "settings": row.get("settings") or {},
    }


# ── Session cookie signing ───────────────────────────────────

def _get_secret() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        raise ValueError("SESSION_SECRET env var is required")
    return secret.encode()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _sign(payload_bytes: bytes, secret: bytes) -> str:
    sig = hmac.new(secret, payload_bytes, hashlib.sha256).digest()
    return _b64url_encode(sig)


def create_session_token(account_id: str, account_name: str) -> str:
    """Create a signed session token for the given account.

    Token format: base64url(json).base64url(hmac-sha256)
    Payload: {"aid": account_id, "name": account_name, "exp": unix_ts}
    """
    secret = _get_secret()
    payload = {
        "aid": account_id,
        "name": account_name,
        "exp": int(time.time()) + SESSION_TTL,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = _b64url_encode(payload_bytes)
    signature = _sign(payload_bytes, secret)
    return f"{payload_b64}.{signature}"


def verify_session_token(token: str) -> dict | None:
    """Verify a session token. Returns {"account_id": ..., "name": ...} or None."""
    secret = _get_secret()

    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts

    try:
        payload_bytes = _b64url_decode(payload_b64)
        expected_sig = _sign(payload_bytes, secret)
        if not hmac.compare_digest(sig_b64, expected_sig):
            return None

        payload = json.loads(payload_bytes)
    except Exception:
        return None

    # Check expiry
    if payload.get("exp", 0) < time.time():
        return None

    return {
        "account_id": payload["aid"],
        "name": payload["name"],
    }


def make_session_cookie(token: str, secure: bool = True) -> str:
    """Build the Set-Cookie header value for a session token."""
    parts = [
        f"{COOKIE_NAME}={token}",
        "HttpOnly",
        "SameSite=Lax",
        f"Path=/",
        f"Max-Age={COOKIE_MAX_AGE}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def make_clear_cookie() -> str:
    """Build the Set-Cookie header value that clears the session."""
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"


# ── Request helpers ───────────────────────────────────────────

def get_account_from_cookie_header(cookie_header: str | None) -> dict | None:
    """Parse the Cookie header and verify the session.

    Returns {"account_id": ..., "name": ...} or None.
    """
    if not cookie_header:
        return None

    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except Exception:
        return None

    morsel = cookies.get(COOKIE_NAME)
    if not morsel:
        return None

    return verify_session_token(morsel.value)


# ── API key helpers ───────────────────────────────────────────

def get_account_keys(client: Client, account_id: str) -> dict:
    """Read API keys from the account's settings JSONB."""
    response = (
        client.table("accounts")
        .select("settings")
        .eq("id", account_id)
        .single()
        .execute()
    )
    settings = response.data.get("settings") or {}
    return {k: settings.get(k, "") for k in API_KEY_FIELDS}


def update_account_keys(client: Client, account_id: str, keys: dict) -> None:
    """Write API keys into the account's settings JSONB (merge, not replace)."""
    # Read current settings to merge
    response = (
        client.table("accounts")
        .select("settings")
        .eq("id", account_id)
        .single()
        .execute()
    )
    settings = response.data.get("settings") or {}

    # Only update recognized key fields
    for k in API_KEY_FIELDS:
        if k in keys:
            settings[k] = keys[k]

    (
        client.table("accounts")
        .update({"settings": settings})
        .eq("id", account_id)
        .execute()
    )


def seed_env_keys(client: Client, account_id: str) -> list[str]:
    """Copy env-level API keys into the account's settings for any key not yet set.

    Called on login/first-use so new accounts inherit platform keys automatically.
    Only seeds keys that exist in env and are missing from the account. Existing
    account keys are never overwritten (user overrides always win).

    Returns the list of keys that were seeded.
    """
    existing = get_account_keys(client, account_id)
    to_seed = {}
    for k in API_KEY_FIELDS:
        if not existing.get(k):
            env_val = os.environ.get(k, "")
            if env_val:
                to_seed[k] = env_val
    if to_seed:
        update_account_keys(client, account_id, to_seed)
    return list(to_seed.keys())


def get_account_api_keys_for_pipeline(client: Client, account_id: str) -> dict:
    """Get API keys in the format the enrichment pipeline expects (env-var names).

    Returns a dict that can be used with os.environ.update() or passed directly.
    """
    return get_account_keys(client, account_id)


# ── Vercel API route helpers ──────────────────────────────────

def json_response(handler, status: int, body: dict) -> None:
    """Send a JSON response from a BaseHTTPRequestHandler."""
    data = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler) -> dict:
    """Read and parse the JSON body from a BaseHTTPRequestHandler."""
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return {}
    raw = handler.rfile.read(content_length)
    return json.loads(raw)


def require_auth(handler) -> dict | None:
    """Check auth on a BaseHTTPRequestHandler. Returns account dict or sends 401.

    Usage in an API route:
        account = require_auth(self)
        if account is None:
            return  # 401 already sent
    """
    cookie_header = handler.headers.get("Cookie")
    account = get_account_from_cookie_header(cookie_header)
    if account is None:
        json_response(handler, 401, {"error": "Not authenticated"})
        return None
    return account
