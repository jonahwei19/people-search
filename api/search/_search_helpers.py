"""Shared helpers for search API routes."""

from api._helpers import get_storage
from cloud.storage.supabase import SupabaseStorage
from search.models import Profile as V2Profile, ProfileIdentity


def get_v2_profiles(storage: SupabaseStorage, dataset_id: str = None) -> list[V2Profile]:
    """Convert enrichment profiles to v2 search profiles for the LLM judge."""
    profiles = []
    for ds_info in storage.list_datasets():
        if dataset_id and ds_info["id"] != dataset_id:
            continue
        ds = storage.load_dataset(ds_info["id"])
        for p in ds.profiles:
            raw_text = p.profile_card or ""
            if not raw_text:
                parts = []
                if p.linkedin_enriched and p.linkedin_enriched.get("context_block"):
                    parts.append(p.linkedin_enriched["context_block"])
                for name, text in p.content_fields.items():
                    if text and text.strip():
                        parts.append(f"{name}: {text.strip()}")
                raw_text = "\n\n".join(parts)

            profiles.append(V2Profile(
                id=p.id,
                dataset_id=ds.id,
                identity=ProfileIdentity(
                    name=p.display_name(),
                    email=p.email or None,
                    linkedin_url=p.linkedin_url or None,
                ),
                raw_text=raw_text[:3000],
            ))
    return profiles
