"""Canonical person_id derivation.

Same logic as migration 005_person_id.sql — keep the two in lockstep so
back-filled ids match newly-assigned ones.

Grouping key order of trust:
    1. email (highest)
    2. linkedin_url
    3. name + organization (fallback, weaker)

Profiles matching on any key share a person_id. Profiles with no matchable
identity get a per-row id so they stay un-merged.
"""

from __future__ import annotations

import hashlib


def group_key(email: str, linkedin_url: str, name: str, organization: str) -> str:
    """Return the grouping key used by person_id derivation. Empty → per-row."""
    e = (email or "").strip().lower()
    li = (linkedin_url or "").strip().lower().rstrip("/")
    nm = (name or "").strip().lower()
    org = (organization or "").strip().lower()
    if e:
        return f"email:{e}"
    if li:
        return f"li:{li}"
    if nm:
        return f"name:{nm}|{org}"
    return ""  # caller must synthesize a per-row fallback


def person_id_for(
    account_id: str,
    email: str = "",
    linkedin_url: str = "",
    name: str = "",
    organization: str = "",
    fallback_row_id: str = "",
) -> str:
    """Deterministic 12-char person_id. Matches migration 005's hash."""
    key = group_key(email, linkedin_url, name, organization)
    if not key:
        key = f"none:{fallback_row_id}"
    digest = hashlib.md5(f"{account_id}:{key}".encode("utf-8")).hexdigest()
    return digest[:12]
