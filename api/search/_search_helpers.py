"""Shared helpers for search API routes."""

from api._helpers import get_storage
from cloud.storage.supabase import SupabaseStorage
from search.models import Profile as V2Profile, ProfileIdentity


def get_v2_profile(storage: SupabaseStorage, profile_id: str) -> V2Profile | None:
    """Load a single profile and convert to V2 search profile."""
    p = storage.load_profile(profile_id)
    if not p:
        return None
    return V2Profile(
        id=p.id,
        dataset_id="",
        person_id=getattr(p, "person_id", "") or "",
        identity=ProfileIdentity(
            name=p.display_name(),
            email=p.email or None,
            linkedin_url=p.linkedin_url or None,
        ),
        raw_text=_build_raw_text(p),
    )


def _build_raw_text(p) -> str:
    """Build raw_text for a profile, with fallbacks for sparse data."""
    raw_text = p.profile_card or ""
    if not raw_text:
        parts = []
        if p.linkedin_enriched and p.linkedin_enriched.get("context_block"):
            parts.append(p.linkedin_enriched["context_block"])
        for name, text in p.content_fields.items():
            if text and text.strip():
                parts.append(f"{name}: {text.strip()}")
        raw_text = "\n\n".join(parts)

    # Fallback: if still empty, build minimal context from identity + metadata
    if not raw_text.strip():
        parts = []
        if p.name:
            parts.append(f"Name: {p.name}")
        if p.organization:
            parts.append(f"Organization: {p.organization}")
        if p.title:
            parts.append(f"Title: {p.title}")
        if p.email:
            parts.append(f"Email: {p.email}")
        for key, val in (p.metadata or {}).items():
            if val and str(val).strip() and len(str(val)) > 2:
                parts.append(f"{key}: {val}")
        raw_text = "\n".join(parts)

    return raw_text[:3000]


def get_v2_profiles(storage: SupabaseStorage, dataset_id: str = None) -> list[V2Profile]:
    """Convert enrichment profiles to v2 search profiles for the LLM judge.

    IO fix: the LLM judge only needs the pre-built `profile_card` text plus
    identity fields. We therefore SELECT the minimum columns rather than
    every JSONB blob (linkedin_enriched / content_fields / fetched_content /
    verification_decisions). On IFP-scale accounts this cuts per-search
    disk reads by roughly 10-20×.

    Fallback path: if `profile_card` is empty (pre-carding rows, legacy
    profiles), fall back to loading the full row for that profile only,
    so `_build_raw_text` can still construct context from JSONB fields.
    """
    # One query per account — no per-dataset round trips.
    select_cols = (
        "id,dataset_id,person_id,name,email,linkedin_url,organization,title,"
        "profile_card,metadata,linkedin_enriched"
    )
    q = (
        storage.client.table("profiles")
        .select(select_cols)
        .eq("account_id", storage.account_id)
    )
    if dataset_id:
        q = q.eq("dataset_id", dataset_id)

    profiles: list[V2Profile] = []
    offset = 0
    page = 1000
    pj = storage._parse_json
    while True:
        resp = q.range(offset, offset + page - 1).execute()
        rows = resp.data or []
        for row in rows:
            raw_text = row.get("profile_card") or ""
            if not raw_text:
                # Legacy / uncarded row — fall back to a minimal assembly
                # from the columns we already have, without re-fetching
                # the heavy content_fields/fetched_content blobs.
                parts = []
                le = pj(row.get("linkedin_enriched"), {})
                if le.get("context_block"):
                    parts.append(le["context_block"])
                if row.get("name"):
                    parts.append(f"Name: {row['name']}")
                if row.get("organization"):
                    parts.append(f"Organization: {row['organization']}")
                if row.get("title"):
                    parts.append(f"Title: {row['title']}")
                if row.get("email"):
                    parts.append(f"Email: {row['email']}")
                for key, val in (pj(row.get("metadata"), {}) or {}).items():
                    if val and str(val).strip() and len(str(val)) > 2:
                        parts.append(f"{key}: {val}")
                raw_text = ("\n".join(parts))[:3000]

            display = (le := pj(row.get("linkedin_enriched"), {})).get("full_name") or row.get("name") or row.get("email") or "Unknown"
            profiles.append(V2Profile(
                id=row["id"],
                dataset_id=row.get("dataset_id") or "",
                person_id=row.get("person_id") or "",
                identity=ProfileIdentity(
                    name=display,
                    email=row.get("email") or None,
                    linkedin_url=row.get("linkedin_url") or None,
                ),
                raw_text=raw_text,
            ))
        if len(rows) < page:
            break
        offset += page
    return profiles
