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
    """Convert enrichment profiles to v2 search profiles for the LLM judge."""
    profiles = []
    for ds_info in storage.list_datasets():
        if dataset_id and ds_info["id"] != dataset_id:
            continue
        ds = storage.load_dataset(ds_info["id"])
        for p in ds.profiles:
            profiles.append(V2Profile(
                id=p.id,
                dataset_id=ds.id,
                identity=ProfileIdentity(
                    name=p.display_name(),
                    email=p.email or None,
                    linkedin_url=p.linkedin_url or None,
                ),
                raw_text=_build_raw_text(p),
            ))
    return profiles
