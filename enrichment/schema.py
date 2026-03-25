"""Auto-detect CSV/JSON schemas and map columns to field types.

The key insight: users upload data with varying richness. Some have just
name+email. Others have full CRM exports with notes, call transcripts,
tags, etc. The schema detector figures out what's there and maps it.

Field types:
- IDENTITY: name, email, linkedin_url, organization, title, phone
  (used for identity resolution and enrichment)
- CONTENT: notes, transcripts, bios, pitches, descriptions
  (arbitrary text → embedded for search)
- METADATA: tags, categories, dates, locations
  (structured → filterable, not embedded)
- IGNORE: IDs, timestamps, internal fields
  (skipped entirely)
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Any


class FieldType(str, Enum):
    IDENTITY_NAME = "identity_name"
    IDENTITY_EMAIL = "identity_email"
    IDENTITY_LINKEDIN = "identity_linkedin"
    IDENTITY_ORG = "identity_org"
    IDENTITY_TITLE = "identity_title"
    IDENTITY_PHONE = "identity_phone"
    LINK_TWITTER = "link_twitter"       # X/Twitter profile URL
    LINK_WEBSITE = "link_website"       # Personal/org website
    LINK_RESUME = "link_resume"         # Google Drive, Dropbox, etc. resume/CV link
    LINK_OTHER = "link_other"           # Other URLs (GitHub, etc.)
    LINKEDIN_TEXT = "linkedin_text"     # Pre-enriched LinkedIn profile text (skip EnrichLayer)
    CONTENT = "content"
    METADATA = "metadata"
    IGNORE = "ignore"


# Patterns for auto-detection. Order matters — first match wins.
COLUMN_PATTERNS: list[tuple[str, FieldType]] = [
    # Identity fields — name (broad: attendee, participant, respondent, etc.)
    (r"(?i)(full.?name|first.?name|last.?name|given.?name|family.?name|^name$|^person$|^candidate$|contact.?name|^attendee$|^participant$|^respondent$|^applicant$|^member$|^speaker$|^panelist$|^author$)", FieldType.IDENTITY_NAME),
    # Email — handle verbose exports like "E-mail 1 - Value"
    (r"(?i)(e[-.]?mail|email.?address)", FieldType.IDENTITY_EMAIL),
    (r"(?i)(linkedin|li.?url|li.?profile|linkedin.?url|linkedin.?profile)", FieldType.IDENTITY_LINKEDIN),
    (r"(?i)(company|org|organization|employer|firm|institution)", FieldType.IDENTITY_ORG),
    # Title — handle "Organization 1 - Title", "Job Title", etc.
    (r"(?i)(^title$|job.?title|^role$|^position$|designation|- title$)", FieldType.IDENTITY_TITLE),
    (r"(?i)(phone|mobile|cell|tel)", FieldType.IDENTITY_PHONE),
    # Social / link fields
    (r"(?i)(twitter|x\.com|x account|x profile|x handle)", FieldType.LINK_TWITTER),
    (r"(?i)(website|personal.?site|homepage|url|web.?page)", FieldType.LINK_WEBSITE),
    (r"(?i)(resume|cv|curriculum.?vitae|gdrive|google.?drive)", FieldType.LINK_RESUME),
    (r"(?i)(github|gitlab|portfolio)", FieldType.LINK_OTHER),

    # Obvious ignore fields
    (r"(?i)^(id|_id|row.?id|record.?id|uuid|index|#)$", FieldType.IGNORE),
    (r"(?i)^(created|updated|modified|timestamp|date.?added|imported)(.?at|.?on|.?date)?$", FieldType.IGNORE),

    # Metadata fields (structured, short values) — checked before content so "score" beats "recommendation"
    (r"(?i)(tag|label|category|type|status|stage|source|location|city|state|country|region)", FieldType.METADATA),
    (r"(?i)(date|year|score|rating|rank|priority|tier)", FieldType.METADATA),

    # Content fields — column names that strongly suggest free-text content
    (r"(?i)(notes?|transcript|call.?notes?|meeting.?notes?|interview|comments?|description|bio|about|summary|pitch|narrative|observations?|assessment|evaluation|recommendation|review)", FieldType.CONTENT),

    # Everything else with text content → content field
]


# Common first names for name-detection heuristic (subset — expand as needed)
_COMMON_FIRST_NAMES = {
    "james", "john", "robert", "michael", "david", "william", "richard", "joseph",
    "thomas", "charles", "mary", "patricia", "jennifer", "linda", "elizabeth",
    "sarah", "jessica", "susan", "margaret", "alice", "jane", "alex", "bob",
    "wei", "yuki", "priya", "anil", "elena", "marcus", "diana", "rachel",
    "tom", "mark", "dr", "col", "lt", "gen", "prof",
}


def _looks_like_person_name(value: str) -> bool:
    """Heuristic: does this value look like a person's name?"""
    if not value or len(value) < 2 or len(value) > 80:
        return False
    # Should be mostly letters and spaces/periods/hyphens
    cleaned = re.sub(r"[.\-',()]", "", value)
    if not all(c.isalpha() or c.isspace() for c in cleaned):
        return False
    words = value.split()
    if len(words) < 1 or len(words) > 6:
        return False
    # At least one word should be capitalized (or be a known title/name)
    first_word = words[0].lower().rstrip(".")
    if first_word in _COMMON_FIRST_NAMES:
        return True
    if any(w[0].isupper() for w in words if w):
        return True
    return False


@dataclass
class FieldMapping:
    """Maps a source column to a field type and target name."""
    source_column: str       # original column name from CSV/JSON
    field_type: FieldType    # what kind of field this is
    target_name: str         # normalized name (e.g., "call_transcript")
    sample_values: list[str] # 3 sample values for user review
    confidence: float        # 0-1, how confident the auto-detection is

    def to_dict(self) -> dict:
        return {
            "source_column": self.source_column,
            "field_type": self.field_type.value,
            "target_name": self.target_name,
            "sample_values": self.sample_values,
            "confidence": self.confidence,
        }


class SchemaDetector:
    """Detects and maps columns from uploaded data."""

    def detect_csv(self, file_path: str | Path) -> list[FieldMapping]:
        """Detect schema from a CSV file."""
        path = Path(file_path)
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or []
            # Read sample rows
            rows = []
            for i, row in enumerate(reader):
                if i >= 20:
                    break
                rows.append(row)
        return self._detect_columns(columns, rows)

    def detect_json(self, file_path: str | Path) -> list[FieldMapping]:
        """Detect schema from a JSON file (list of objects)."""
        path = Path(file_path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = [data]
        if not data:
            return []
        columns = list(data[0].keys())
        rows = [{k: str(v) if v is not None else "" for k, v in row.items()} for row in data[:20]]
        return self._detect_columns(columns, rows)

    def detect_from_text(self, csv_text: str) -> list[FieldMapping]:
        """Detect schema from CSV text content."""
        reader = csv.DictReader(StringIO(csv_text))
        columns = reader.fieldnames or []
        rows = []
        for i, row in enumerate(reader):
            if i >= 20:
                break
            rows.append(row)
        return self._detect_columns(columns, rows)

    def _detect_columns(self, columns: list[str], sample_rows: list[dict]) -> list[FieldMapping]:
        """Core detection logic."""
        mappings = []
        used_types = set()  # track which identity types are already assigned

        for col in columns:
            # Collect up to 3 non-empty samples from all available rows
            samples = [row.get(col, "") for row in sample_rows if row.get(col, "")][:3]

            field_type, confidence = self._classify_column(col, samples, used_types)

            # Track identity types so we don't double-assign
            if field_type.value.startswith("identity_"):
                used_types.add(field_type)

            target_name = self._normalize_name(col, field_type)

            mappings.append(FieldMapping(
                source_column=col,
                field_type=field_type,
                target_name=target_name,
                sample_values=samples[:3],
                confidence=confidence,
            ))

        return mappings

    def _classify_column(
        self, col_name: str, samples: list[str], used_types: set[FieldType]
    ) -> tuple[FieldType, float]:
        """Classify a column by name patterns and sample data."""

        # 1. Try name-based pattern matching, but validate against data
        for pattern, ftype in COLUMN_PATTERNS:
            if re.search(pattern, col_name):
                # Don't double-assign identity types — except names
                # (First Name + Last Name should both be identity_name)
                # But for LinkedIn: check if it's pre-enriched text before skipping
                if ftype == FieldType.IDENTITY_LINKEDIN and ftype in used_types and samples:
                    # Already have a LinkedIn URL column — check if this one is pre-enriched text
                    non_empty = [s for s in samples if s and len(s) > 100]
                    if non_empty and any(
                        "experience" in s.lower() or "education" in s.lower()
                        or "headline" in s.lower() for s in non_empty
                    ):
                        return FieldType.LINKEDIN_TEXT, 0.85
                    continue
                if ftype in used_types and ftype.value.startswith("identity_") and ftype != FieldType.IDENTITY_NAME:
                    continue

                # Validate: if the column name says "LinkedIn" but the values
                # are "Yes/No" or other non-URL text, the column name is lying
                if ftype == FieldType.IDENTITY_LINKEDIN and samples:
                    if not any("linkedin.com" in s.lower() for s in samples if s):
                        # Check if it's pre-enriched LinkedIn text (long text with profile data)
                        non_empty = [s for s in samples if s and len(s) > 100]
                        if non_empty and any(
                            "experience" in s.lower() or "education" in s.lower()
                            or "headline" in s.lower() for s in non_empty
                        ):
                            return FieldType.LINKEDIN_TEXT, 0.85
                        # Otherwise just metadata ("Yes"/"No")
                        continue

                # Validate: if column says "email" but values aren't emails
                if ftype == FieldType.IDENTITY_EMAIL and samples:
                    if not any("@" in s for s in samples if s):
                        continue

                return ftype, 0.9

        # 2. Try data-based heuristics
        if samples:
            # Check if values look like person names (catches "Attendee", "Speaker", etc.)
            if (FieldType.IDENTITY_NAME not in used_types
                    and all(_looks_like_person_name(s) for s in samples if s)):
                return FieldType.IDENTITY_NAME, 0.7

            # Check if values look like emails
            if all(re.match(r"[^@]+@[^@]+\.[^@]+", s) for s in samples if s):
                if FieldType.IDENTITY_EMAIL not in used_types:
                    return FieldType.IDENTITY_EMAIL, 0.8

            # Check if values look like LinkedIn URLs
            if all("linkedin.com" in s.lower() for s in samples if s):
                if FieldType.IDENTITY_LINKEDIN not in used_types:
                    return FieldType.IDENTITY_LINKEDIN, 0.8

            # Check for other specific URL patterns
            if samples and all(re.match(r"https?://", s) for s in samples if s):
                non_empty = [s for s in samples if s]
                if non_empty:
                    if all("twitter.com" in s.lower() or "x.com" in s.lower() for s in non_empty):
                        return FieldType.LINK_TWITTER, 0.8
                    if all("drive.google.com" in s.lower() or "docs.google.com" in s.lower()
                           or "dropbox.com" in s.lower() for s in non_empty):
                        return FieldType.LINK_RESUME, 0.7
                    if all("github.com" in s.lower() or "gitlab.com" in s.lower() for s in non_empty):
                        return FieldType.LINK_OTHER, 0.7
                    # Generic URLs — store as link, user can reclassify
                    return FieldType.LINK_WEBSITE, 0.5

            # Check if values look like phone numbers
            if all(re.match(r"[\d\s\-\+\(\)]{7,15}$", s.strip()) for s in samples if s):
                if FieldType.IDENTITY_PHONE not in used_types:
                    return FieldType.IDENTITY_PHONE, 0.7

            # Check average text length to distinguish content vs metadata
            avg_len = sum(len(s) for s in samples) / max(len(samples), 1)
            if avg_len > 200:
                return FieldType.CONTENT, 0.7
            elif avg_len < 30:
                return FieldType.METADATA, 0.5

        # 3. Default: if it has text, treat as content
        if samples and any(len(s) > 50 for s in samples):
            return FieldType.CONTENT, 0.5

        return FieldType.METADATA, 0.3

    def _normalize_name(self, col_name: str, field_type: FieldType) -> str:
        """Produce a clean target field name."""
        if field_type.value.startswith("identity_"):
            # Identity fields map to fixed names
            return field_type.value.replace("identity_", "")

        # For content/metadata: snake_case the column name
        name = col_name.strip().lower()
        name = re.sub(r"[^a-z0-9]+", "_", name)
        name = name.strip("_")
        return name or "field"
