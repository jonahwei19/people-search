"""Data models for Search V2."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


# --- Profile ---

class ProfileField(BaseModel):
    value: str
    type: str  # "first_person" | "expert_assessment" | "linkedin" | "self_reported" | "metadata"


class ProfileIdentity(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    dataset_id: str = ""
    identity: ProfileIdentity = Field(default_factory=ProfileIdentity)
    fields: dict[str, ProfileField] = Field(default_factory=dict)
    raw_text: str = ""

    def rebuild_raw_text(self, field_priority: list[str] | None = None) -> None:
        """Regenerate raw_text from all content fields.

        If field_priority is given, those fields come first (highest signal first
        so truncation doesn't cut the most important info).
        """
        ordered_names = []
        if field_priority:
            for name in field_priority:
                if name in self.fields:
                    ordered_names.append(name)
        # Add any remaining fields not in the priority list
        for name in self.fields:
            if name not in ordered_names:
                ordered_names.append(name)

        parts = []
        for name in ordered_names:
            field = self.fields[name]
            if field.type != "metadata" and field.value.strip():
                parts.append(f"{name}:\n{field.value.strip()}")
        self.raw_text = "\n\n".join(parts)


# --- Global Rules ---

class GlobalRule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str  # "When [condition], [rule]"
    scope: str = "all searches"  # when does this rule fire
    source: str = "manual"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GlobalRules(BaseModel):
    rules: list[GlobalRule] = Field(default_factory=list)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.model_dump(mode="json"), f, indent=2, default=str)

    @classmethod
    def load(cls, path: str) -> GlobalRules:
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            return cls.model_validate(json.load(f))


# --- Defined Search ---

class Exemplar(BaseModel):
    profile_id: str
    profile_name: str = ""
    profile_summary: str = ""  # condensed profile text for the judge prompt
    score: int  # 1-5
    reason: str


class FeedbackEvent(BaseModel):
    profile_id: str
    profile_name: str = ""
    rating: str  # "strong_yes" | "yes" | "no" | "strong_no"
    reason: Optional[str] = None
    reasoning_correction: Optional[str] = None  # user-edited judge reasoning
    scope: str = "search"  # "search" | "global"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScoreResult(BaseModel):
    score: int  # 0-100
    reasoning: str


class SearchCache(BaseModel):
    prompt_hash: str = ""
    scores: dict[str, ScoreResult] = Field(default_factory=dict)  # profile_id -> score


class DefinedSearch(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    query: str
    clarification_context: str = ""  # answers from clarifying questions
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    search_rules: list[str] = Field(default_factory=list)
    exemplars: list[Exemplar] = Field(default_factory=list)
    feedback_log: list[FeedbackEvent] = Field(default_factory=list)
    cache: SearchCache = Field(default_factory=SearchCache)

    # Track which global rules were deemed relevant (cached)
    applicable_global_rule_ids: list[str] = Field(default_factory=list)

    # Profiles hidden from results (not negative feedback — just removed from view)
    excluded_profile_ids: list[str] = Field(default_factory=list)

    def compute_prompt_hash(self, global_rules: list[GlobalRule]) -> str:
        """Hash of everything that affects scoring — used for cache invalidation."""
        relevant_globals = [r.text for r in global_rules if r.id in self.applicable_global_rule_ids]
        content = json.dumps({
            "query": self.query,
            "clarification": self.clarification_context,
            "search_rules": self.search_rules,
            "exemplars": [e.model_dump(mode="json") for e in self.exemplars],
            "global_rules": relevant_globals,
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def save(self, directory: str) -> None:
        path = os.path.join(directory, f"{self.id}.json")
        with open(path, "w") as f:
            json.dump(self.model_dump(mode="json"), f, indent=2, default=str)

    @classmethod
    def load(cls, path: str) -> DefinedSearch:
        with open(path) as f:
            return cls.model_validate(json.load(f))

    @classmethod
    def load_all(cls, directory: str) -> list[DefinedSearch]:
        searches = []
        if not os.path.exists(directory):
            return searches
        for fname in os.listdir(directory):
            if fname.endswith(".json"):
                searches.append(cls.load(os.path.join(directory, fname)))
        return searches
