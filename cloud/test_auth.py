#!/usr/bin/env python3
"""Test the auth system — both offline (cookie signing) and online (Supabase).

Offline tests (no env vars needed):
    python3 cloud/test_auth.py --offline

Full tests (requires SUPABASE_URL, SUPABASE_SERVICE_KEY, SESSION_SECRET in env):
    python3 cloud/test_auth.py

Run from the candidate-search-tool/ directory.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_offline():
    """Test cookie signing/verification without Supabase."""
    # Set a test secret
    os.environ["SESSION_SECRET"] = "test-secret-key-for-unit-tests"

    from cloud.auth import (
        create_session_token,
        verify_session_token,
        make_session_cookie,
        make_clear_cookie,
        get_account_from_cookie_header,
        COOKIE_NAME,
    )

    print("=== Offline Auth Tests ===\n")

    # 1. Token creation + verification
    token = create_session_token("acc-123", "TestOrg")
    result = verify_session_token(token)
    assert result is not None, "Valid token should verify"
    assert result["account_id"] == "acc-123"
    assert result["name"] == "TestOrg"
    print("[PASS] Token creation and verification")

    # 2. Tampered token fails
    tampered = token[:-3] + "xxx"
    assert verify_session_token(tampered) is None, "Tampered token should fail"
    print("[PASS] Tampered token rejected")

    # 3. Expired token fails
    from cloud import auth as auth_mod
    original_ttl = auth_mod.SESSION_TTL
    auth_mod.SESSION_TTL = -1  # expire immediately
    expired_token = create_session_token("acc-123", "TestOrg")
    auth_mod.SESSION_TTL = original_ttl
    assert verify_session_token(expired_token) is None, "Expired token should fail"
    print("[PASS] Expired token rejected")

    # 4. Garbage token fails
    assert verify_session_token("garbage") is None
    assert verify_session_token("") is None
    assert verify_session_token("a.b.c") is None
    print("[PASS] Garbage tokens rejected")

    # 5. Cookie header parsing
    cookie_header = f"{COOKIE_NAME}={token}"
    result = get_account_from_cookie_header(cookie_header)
    assert result is not None
    assert result["account_id"] == "acc-123"
    print("[PASS] Cookie header parsing works")

    # 6. Cookie header with multiple cookies
    cookie_header = f"other=abc; {COOKIE_NAME}={token}; another=xyz"
    result = get_account_from_cookie_header(cookie_header)
    assert result is not None
    assert result["account_id"] == "acc-123"
    print("[PASS] Cookie extracted from multi-cookie header")

    # 7. Missing cookie returns None
    assert get_account_from_cookie_header(None) is None
    assert get_account_from_cookie_header("other=abc") is None
    print("[PASS] Missing cookie returns None")

    # 8. Set-Cookie format
    cookie = make_session_cookie(token, secure=True)
    assert f"{COOKIE_NAME}={token}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie
    assert "Max-Age=" in cookie
    print("[PASS] Set-Cookie format correct")

    # 9. Clear cookie
    clear = make_clear_cookie()
    assert f"{COOKIE_NAME}=" in clear
    assert "Max-Age=0" in clear
    print("[PASS] Clear cookie format correct")

    # 10. Different secrets produce different tokens
    os.environ["SESSION_SECRET"] = "secret-A"
    token_a = create_session_token("acc-123", "Org")
    os.environ["SESSION_SECRET"] = "secret-B"
    assert verify_session_token(token_a) is None, "Wrong secret should reject"
    print("[PASS] Different secrets are isolated")

    # Restore
    os.environ["SESSION_SECRET"] = "test-secret-key-for-unit-tests"

    print("\n=== All offline tests passed ===\n")


def test_online():
    """Test against real Supabase (requires env vars)."""
    required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"Skipping online tests — missing env vars: {', '.join(missing)}")
        return False

    if not os.environ.get("SESSION_SECRET"):
        os.environ["SESSION_SECRET"] = "test-secret-for-online"

    from cloud.auth import (
        get_supabase_client,
        verify_login,
        create_session_token,
        verify_session_token,
        get_account_keys,
        update_account_keys,
    )

    print("=== Online Auth Tests (Supabase) ===\n")

    client = get_supabase_client()
    test_name = f"_test_auth_{int(time.time())}"
    test_password = "test-pass-123"
    account_id = None

    try:
        # 1. Create a test account via RPC
        resp = client.rpc(
            "create_account",
            {"p_name": test_name, "p_password": test_password},
        ).execute()
        account_id = resp.data
        assert account_id, "create_account should return an ID"
        print(f"[PASS] Created test account: {test_name} (id={account_id})")

        # 2. Verify correct login
        result = verify_login(client, test_name, test_password)
        assert result is not None, "Correct credentials should succeed"
        assert result["id"] == account_id
        assert result["name"] == test_name
        print("[PASS] verify_login with correct password")

        # 3. Verify wrong password fails
        result = verify_login(client, test_name, "wrong-password")
        assert result is None, "Wrong password should fail"
        print("[PASS] verify_login rejects wrong password")

        # 4. Verify nonexistent account fails
        result = verify_login(client, "nonexistent-org-xyz", test_password)
        assert result is None, "Nonexistent account should fail"
        print("[PASS] verify_login rejects nonexistent account")

        # 5. Full flow: login → token → verify
        result = verify_login(client, test_name, test_password)
        token = create_session_token(result["id"], result["name"])
        session = verify_session_token(token)
        assert session["account_id"] == account_id
        print("[PASS] Full login → token → verify flow")

        # 6. API keys: read defaults (empty)
        keys = get_account_keys(client, account_id)
        assert all(v == "" for v in keys.values()), "Default keys should be empty"
        print("[PASS] Default API keys are empty")

        # 7. API keys: write and read back
        test_keys = {
            "BRAVE_API_KEY": "brave-test-key",
            "GOOGLE_API_KEY": "google-test-key",
        }
        update_account_keys(client, account_id, test_keys)
        keys = get_account_keys(client, account_id)
        assert keys["BRAVE_API_KEY"] == "brave-test-key"
        assert keys["GOOGLE_API_KEY"] == "google-test-key"
        assert keys["SERPER_API_KEY"] == ""  # untouched
        print("[PASS] API keys write and read back")

        # 8. API keys: partial update doesn't clobber
        update_account_keys(client, account_id, {"SERPER_API_KEY": "serper-test"})
        keys = get_account_keys(client, account_id)
        assert keys["BRAVE_API_KEY"] == "brave-test-key"  # still there
        assert keys["SERPER_API_KEY"] == "serper-test"
        print("[PASS] Partial key update preserves existing keys")

        # 9. Account isolation: create second account, verify no cross-access
        test_name_b = f"_test_auth_b_{int(time.time())}"
        resp_b = client.rpc(
            "create_account",
            {"p_name": test_name_b, "p_password": "pass-b"},
        ).execute()
        account_id_b = resp_b.data

        keys_b = get_account_keys(client, account_id_b)
        assert all(v == "" for v in keys_b.values()), "Account B should have empty keys"
        print("[PASS] Account isolation — B cannot see A's keys")

        # Clean up account B
        client.table("accounts").delete().eq("id", account_id_b).execute()

        print(f"\n=== All online tests passed ===\n")

    finally:
        # Clean up test account
        if account_id:
            client.table("accounts").delete().eq("id", account_id).execute()
            print(f"Cleaned up test account: {test_name}")

    return True


if __name__ == "__main__":
    offline_only = "--offline" in sys.argv

    test_offline()

    if not offline_only:
        test_online()
