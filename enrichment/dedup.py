"""Cross-dataset deduplication.

When uploading new data, check against all existing datasets for
duplicate profiles. Match by email (exact), LinkedIn URL (exact),
and name+org (fuzzy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import Dataset, Profile


@dataclass
class DedupMatch:
    """A potential duplicate found across datasets."""
    new_profile_idx: int         # index in the new upload
    new_name: str
    existing_profile_id: str
    existing_name: str
    existing_dataset_id: str
    existing_dataset_name: str
    match_type: str              # "email" | "linkedin" | "name_org"
    confidence: float            # 0-1

    def to_dict(self) -> dict:
        return {
            "new_profile_idx": self.new_profile_idx,
            "new_name": self.new_name,
            "existing_profile_id": self.existing_profile_id,
            "existing_name": self.existing_name,
            "existing_dataset_id": self.existing_dataset_id,
            "existing_dataset_name": self.existing_dataset_name,
            "match_type": self.match_type,
            "confidence": self.confidence,
        }


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching."""
    if not name:
        return ""
    # Remove titles, punctuation, extra whitespace
    name = re.sub(r"(?i)\b(dr|prof|mr|mrs|ms|sir|col|lt|gen|ret|jr|sr|iii?|iv)\b\.?", "", name)
    name = re.sub(r"[^a-zA-Z\s]", "", name)
    name = " ".join(name.split()).lower().strip()
    return name


def _normalize_email(email: str) -> str:
    return email.strip().lower() if email else ""


def _normalize_linkedin(url: str) -> str:
    if not url:
        return ""
    url = url.strip().lower().rstrip("/")
    url = re.sub(r"[?#].*$", "", url)
    return url


def _normalize_org(org: str) -> str:
    if not org:
        return ""
    org = re.sub(r"(?i)\b(inc|llc|ltd|corp|co|company|the|of|for)\b\.?", "", org)
    org = re.sub(r"[^a-zA-Z\s]", "", org)
    org = " ".join(org.split()).lower().strip()
    return org


def find_duplicates(
    new_profiles: list[Profile],
    existing_datasets: list[Dataset],
) -> list[DedupMatch]:
    """Find potential duplicates between new profiles and existing datasets.

    Returns matches sorted by confidence (highest first).
    """
    # Build lookup indexes from existing data
    email_index: dict[str, tuple[Profile, Dataset]] = {}
    linkedin_index: dict[str, tuple[Profile, Dataset]] = {}
    name_org_index: dict[str, list[tuple[Profile, Dataset]]] = {}

    for ds in existing_datasets:
        for p in ds.profiles:
            if p.email:
                email_index[_normalize_email(p.email)] = (p, ds)
            if p.linkedin_url:
                linkedin_index[_normalize_linkedin(p.linkedin_url)] = (p, ds)
            norm_name = _normalize_name(p.name)
            if norm_name:
                key = norm_name
                if key not in name_org_index:
                    name_org_index[key] = []
                name_org_index[key].append((p, ds))

    matches = []
    seen = set()  # avoid duplicate matches for same new profile

    for i, new_p in enumerate(new_profiles):
        # 1. Email match (definitive)
        if new_p.email:
            norm_email = _normalize_email(new_p.email)
            if norm_email in email_index:
                existing_p, existing_ds = email_index[norm_email]
                key = (i, existing_p.id)
                if key not in seen:
                    seen.add(key)
                    matches.append(DedupMatch(
                        new_profile_idx=i,
                        new_name=new_p.name or new_p.email,
                        existing_profile_id=existing_p.id,
                        existing_name=existing_p.display_name(),
                        existing_dataset_id=existing_ds.id,
                        existing_dataset_name=existing_ds.name,
                        match_type="email",
                        confidence=1.0,
                    ))

        # 2. LinkedIn URL match (definitive)
        if new_p.linkedin_url:
            norm_li = _normalize_linkedin(new_p.linkedin_url)
            if norm_li in linkedin_index:
                existing_p, existing_ds = linkedin_index[norm_li]
                key = (i, existing_p.id)
                if key not in seen:
                    seen.add(key)
                    matches.append(DedupMatch(
                        new_profile_idx=i,
                        new_name=new_p.name or new_p.email,
                        existing_profile_id=existing_p.id,
                        existing_name=existing_p.display_name(),
                        existing_dataset_id=existing_ds.id,
                        existing_dataset_name=existing_ds.name,
                        match_type="linkedin",
                        confidence=1.0,
                    ))

        # 3. Name + org match (fuzzy)
        norm_name = _normalize_name(new_p.name)
        if norm_name and norm_name in name_org_index:
            for existing_p, existing_ds in name_org_index[norm_name]:
                key = (i, existing_p.id)
                if key in seen:
                    continue

                # Name matches — check org for confirmation
                new_org = _normalize_org(new_p.organization)
                existing_org = _normalize_org(existing_p.organization)

                if new_org and existing_org:
                    # Both have orgs — check if they match
                    if new_org == existing_org or new_org in existing_org or existing_org in new_org:
                        confidence = 0.9
                    else:
                        confidence = 0.5  # same name, different org
                else:
                    confidence = 0.6  # name match only

                seen.add(key)
                matches.append(DedupMatch(
                    new_profile_idx=i,
                    new_name=new_p.name or new_p.email,
                    existing_profile_id=existing_p.id,
                    existing_name=existing_p.display_name(),
                    existing_dataset_id=existing_ds.id,
                    existing_dataset_name=existing_ds.name,
                    match_type="name_org",
                    confidence=confidence,
                ))

    matches.sort(key=lambda m: -m.confidence)
    return matches


def load_all_datasets(data_dir: Path) -> list[Dataset]:
    """Load all datasets from the data directory."""
    datasets = []
    for path in sorted(data_dir.glob("*.json")):
        try:
            datasets.append(Dataset.load(path))
        except Exception:
            continue
    return datasets
