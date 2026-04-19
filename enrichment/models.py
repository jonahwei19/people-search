"""Data models for datasets and profiles."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class EnrichmentStatus(str, Enum):
    PENDING = "pending"
    ENRICHED = "enriched"
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g., no LinkedIn URL found


@dataclass
class Profile:
    """A normalized person profile. Core identity + arbitrary content fields."""

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    email: str = ""
    linkedin_url: str = ""
    organization: str = ""
    title: str = ""
    phone: str = ""

    # Links (social, resume, website, etc.)
    twitter_url: str = ""
    website_url: str = ""
    resume_url: str = ""
    other_links: list[str] = field(default_factory=list)

    # Enriched LinkedIn data (populated after enrichment)
    linkedin_enriched: dict = field(default_factory=dict)
    # Keys: full_name, headline, current_company, current_title, location,
    #        summary, experience (list), education (list), context_block (str)

    # Arbitrary content fields from upload (notes, transcripts, bios, etc.)
    # Each key is the field name, value is the text content.
    content_fields: dict[str, str] = field(default_factory=dict)

    # Metadata fields (tags, categories, dates — structured, filterable)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Summarized profile card (compact text for LLM scoring)
    profile_card: str = ""
    field_summaries: dict[str, str] = field(default_factory=dict)

    # Fetched link content (text pulled from Twitter, GitHub, websites, etc.)
    fetched_content: dict[str, str] = field(default_factory=dict)

    # Pipeline state
    enrichment_status: EnrichmentStatus = EnrichmentStatus.PENDING
    enrichment_log: list[str] = field(default_factory=list)  # per-profile log of what happened
    enrichment_version: str = ""  # pipeline generation that produced this record.
    #   "" — pending / never run through any pipeline
    #   "v0-legacy" — produced before the version stamp existed (back-filled by migration 002)
    #   "v1", "v2", … — produced by pipeline.run_enrichment() with matching ENRICHMENT_VERSION
    source_dataset: str = ""
    source_row: int = -1

    def searchable_text_fields(self) -> dict[str, str]:
        """All text fields available for search.

        Returns a dict of field_name → text. Includes LinkedIn,
        user-provided content fields, and fetched link content.
        """
        fields = {}

        # LinkedIn profile text (from enrichment)
        if self.linkedin_enriched.get("context_block"):
            fields["linkedin"] = self.linkedin_enriched["context_block"]

        # All user-provided content fields
        for name, text in self.content_fields.items():
            if text and text.strip():
                fields[name] = text.strip()

        # Fetched link content (GitHub bio, website text, etc.)
        for name, text in self.fetched_content.items():
            if text and text.strip():
                fields[f"fetched_{name}"] = text.strip()

        return fields

    def build_raw_text(self, use_llm: bool = False) -> str:
        """Build compact raw_text for v2 LLM judge scoring.

        Uses the summarizer to compress verbose fields and prioritize
        high-value content (call notes > LinkedIn > pitches).
        """
        from .summarizer import build_profile_card

        # Merge fetched content into content_fields for summarization
        all_content = dict(self.content_fields)
        for name, text in self.fetched_content.items():
            if text and text.strip():
                all_content[f"fetched_{name}"] = text

        card, summaries = build_profile_card(
            name=self.display_name(),
            title=self.title,
            organization=self.organization,
            content_fields=all_content,
            linkedin_enriched=self.linkedin_enriched or None,
            use_llm=use_llm,
        )
        self.profile_card = card
        self.field_summaries = summaries
        return card

    def display_name(self) -> str:
        enriched_name = self.linkedin_enriched.get("full_name", "")
        return enriched_name or self.name or self.email or "Unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["enrichment_status"] = self.enrichment_status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Profile:
        d = dict(d)
        if "enrichment_status" in d:
            d["enrichment_status"] = EnrichmentStatus(d["enrichment_status"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Dataset:
    """A named collection of profiles from a single upload."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # Schema mapping used for this dataset
    field_mappings: list[dict] = field(default_factory=list)

    # All profiles in this dataset
    profiles: list[Profile] = field(default_factory=list)

    # Upload metadata
    source_file: str = ""
    total_rows: int = 0

    # Enrichment summary
    enrichment_stats: dict = field(default_factory=dict)

    # Text fields available for search (computed after processing)
    # e.g., ["linkedin", "notes", "call_transcript", "pitch"]
    searchable_fields: list[str] = field(default_factory=list)

    def save(self, path: Path):
        """Save dataset to JSON."""
        data = {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "field_mappings": self.field_mappings,
            "source_file": self.source_file,
            "total_rows": self.total_rows,
            "enrichment_stats": self.enrichment_stats,
            "searchable_fields": self.searchable_fields,
            "profiles": [p.to_dict() for p in self.profiles],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> Dataset:
        """Load dataset from JSON."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        profiles = [Profile.from_dict(p) for p in data.pop("profiles", [])]
        ds = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        ds.profiles = profiles
        return ds
