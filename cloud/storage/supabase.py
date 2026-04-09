"""Supabase storage adapter for People Search.

Drop-in replacement for file-based storage. Uses the Supabase service key
(bypasses RLS) and filters by account_id in all queries.

Assumes the project root (candidate-search-tool/) is on sys.path so that
`enrichment.models` and `search.models` are importable.

Requires: pip install supabase
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from supabase import create_client, Client
except ImportError:
    raise ImportError("supabase package required: pip install supabase")

from enrichment.models import Dataset, Profile, EnrichmentStatus
from search.models import (
    DefinedSearch,
    Exemplar,
    FeedbackEvent,
    GlobalRule,
    ScoreResult,
    SearchCache,
)


class SupabaseStorage:
    """Supabase-backed storage for People Search.

    All methods scope queries to self.account_id. Uses the service key
    to bypass RLS — row-level security still protects direct DB access
    via the anon key.
    """

    BATCH_SIZE = 500   # max rows per upsert
    PAGE_SIZE = 1000   # max rows per select (PostgREST default)

    def __init__(self, supabase_url: str, supabase_key: str, account_id: str):
        self.client: Client = create_client(supabase_url, supabase_key)
        self.account_id = account_id

    # ── Dataset operations ─────────────────────────────────────

    def save_dataset(self, dataset: Dataset) -> None:
        """Save dataset metadata and all profiles."""
        row = {
            "id": dataset.id,
            "account_id": self.account_id,
            "name": dataset.name,
            "source_file": dataset.source_file or "",
            "total_rows": dataset.total_rows,
            "field_mappings": dataset.field_mappings,
            "searchable_fields": dataset.searchable_fields,
            "enrichment_stats": dataset.enrichment_stats,
            "created_at": dataset.created_at,
        }
        self.client.table("datasets").upsert(row).execute()

        if dataset.profiles:
            self.save_profiles(dataset.id, dataset.profiles)

    def load_dataset(self, dataset_id: str) -> Dataset:
        """Load a dataset with all its profiles."""
        response = (
            self.client.table("datasets")
            .select("*")
            .eq("id", dataset_id)
            .eq("account_id", self.account_id)
            .single()
            .execute()
        )
        row = response.data
        profiles = self.load_profiles(dataset_id)

        return Dataset(
            id=row["id"],
            name=row["name"],
            created_at=row.get("created_at", ""),
            field_mappings=row.get("field_mappings") or [],
            profiles=profiles,
            source_file=row.get("source_file", ""),
            total_rows=row.get("total_rows", 0),
            enrichment_stats=row.get("enrichment_stats") or {},
            searchable_fields=row.get("searchable_fields") or [],
        )

    def list_datasets(self) -> list[dict]:
        """List all datasets for the account with profile counts."""
        response = (
            self.client.table("datasets")
            .select("id, name, created_at, source_file, searchable_fields")
            .eq("account_id", self.account_id)
            .order("created_at", desc=True)
            .execute()
        )

        # Single RPC call for all profile counts
        counts_resp = self.client.rpc(
            "dataset_profile_counts",
            {"p_account_id": self.account_id},
        ).execute()
        counts = {
            r["dataset_id"]: r["profile_count"]
            for r in (counts_resp.data or [])
        }

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "profiles": counts.get(row["id"], 0),
                "created_at": row.get("created_at", ""),
                "source_file": row.get("source_file", ""),
                "searchable_fields": row.get("searchable_fields") or [],
            }
            for row in response.data
        ]

    def delete_dataset(self, dataset_id: str) -> None:
        """Delete a dataset and all its profiles (CASCADE)."""
        (
            self.client.table("datasets")
            .delete()
            .eq("id", dataset_id)
            .eq("account_id", self.account_id)
            .execute()
        )

    # ── Profile operations ─────────────────────────────────────

    def save_profiles(self, dataset_id: str, profiles: list[Profile]) -> None:
        """Bulk upsert profiles for a dataset."""
        if not profiles:
            return
        rows = [self._profile_to_row(p, dataset_id) for p in profiles]
        for i in range(0, len(rows), self.BATCH_SIZE):
            batch = rows[i : i + self.BATCH_SIZE]
            self.client.table("profiles").upsert(batch).execute()

    def load_profiles(self, dataset_id: str) -> list[Profile]:
        """Load all profiles for a dataset (paginated)."""
        profiles: list[Profile] = []
        offset = 0

        while True:
            response = (
                self.client.table("profiles")
                .select("*")
                .eq("dataset_id", dataset_id)
                .eq("account_id", self.account_id)
                .range(offset, offset + self.PAGE_SIZE - 1)
                .execute()
            )
            for row in response.data:
                profiles.append(self._row_to_profile(row))

            if len(response.data) < self.PAGE_SIZE:
                break
            offset += self.PAGE_SIZE

        return profiles

    def load_profile(self, profile_id: str) -> Profile | None:
        """Load a single profile by ID."""
        response = (
            self.client.table("profiles")
            .select("*")
            .eq("id", profile_id)
            .eq("account_id", self.account_id)
            .execute()
        )
        if not response.data:
            return None
        return self._row_to_profile(response.data[0])

    def update_profile(self, profile: Profile) -> None:
        """Update a single profile in place (by ID)."""
        row = self._profile_to_row(profile)
        # Don't overwrite ownership columns
        row.pop("dataset_id", None)
        row.pop("account_id", None)
        (
            self.client.table("profiles")
            .update(row)
            .eq("id", profile.id)
            .eq("account_id", self.account_id)
            .execute()
        )

    # ── Search operations ──────────────────────────────────────

    def save_search(self, search: DefinedSearch) -> None:
        """Save search metadata (feedback is stored separately)."""
        row = self._search_to_row(search)
        self.client.table("searches").upsert(row).execute()

    def load_search(self, search_id: str, include_scores: bool = True) -> DefinedSearch | None:
        """Load a search with its feedback events.

        Args:
            include_scores: If False, skip loading cache_scores (much faster
                for large searches). The cache.scores dict will be empty.
        """
        if include_scores:
            select = "*"
        else:
            select = "id, name, query, clarification_context, created_at, search_rules, exemplars, applicable_global_rule_ids, excluded_profile_ids, prompt_corrections, cache_prompt_hash"
        response = (
            self.client.table("searches")
            .select(select)
            .eq("id", search_id)
            .eq("account_id", self.account_id)
            .execute()
        )
        if not response.data:
            return None

        row = response.data[0]
        if not include_scores:
            row["cache_scores"] = {}
        feedback = self.get_feedback(search_id)
        return self._row_to_search(row, feedback)

    def list_searches(self) -> list[dict]:
        """List all searches for the account."""
        response = (
            self.client.table("searches")
            .select("id, name, query, search_rules, cache_scores, created_at")
            .eq("account_id", self.account_id)
            .order("created_at", desc=True)
            .execute()
        )

        # Single RPC call for all feedback counts
        counts_resp = self.client.rpc(
            "search_feedback_counts",
            {"p_account_id": self.account_id},
        ).execute()
        feedback_counts = {
            r["search_id"]: r["feedback_count"]
            for r in (counts_resp.data or [])
        }

        pj = self._parse_json
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "query": row["query"],
                "feedback_count": feedback_counts.get(row["id"], 0),
                "rule_count": len(pj(row.get("search_rules"), [])),
                "search_rules": pj(row.get("search_rules"), []),
                "has_results": bool(pj(row.get("cache_scores"), {})),
            }
            for row in response.data
        ]

    def delete_search(self, search_id: str) -> None:
        """Delete a search and its feedback (CASCADE)."""
        (
            self.client.table("searches")
            .delete()
            .eq("id", search_id)
            .eq("account_id", self.account_id)
            .execute()
        )

    # ── Feedback ───────────────────────────────────────────────

    def add_feedback(self, search_id: str, event: FeedbackEvent) -> None:
        """Insert a single feedback event."""
        row = self._feedback_to_row(search_id, event)
        self.client.table("feedback").insert(row).execute()

    def get_feedback(self, search_id: str) -> list[FeedbackEvent]:
        """Load all feedback events for a search, ordered by time."""
        response = (
            self.client.table("feedback")
            .select("*")
            .eq("search_id", search_id)
            .eq("account_id", self.account_id)
            .order("created_at")
            .execute()
        )
        return [self._row_to_feedback(r) for r in response.data]

    # ── Global rules ───────────────────────────────────────────

    def save_rules(self, rules: list[GlobalRule]) -> None:
        """Replace all global rules for the account."""
        # Delete existing rules
        (
            self.client.table("global_rules")
            .delete()
            .eq("account_id", self.account_id)
            .execute()
        )
        # Insert new set
        if rules:
            rows = [self._rule_to_row(r) for r in rules]
            self.client.table("global_rules").insert(rows).execute()

    def load_rules(self) -> list[GlobalRule]:
        """Load all global rules for the account."""
        response = (
            self.client.table("global_rules")
            .select("*")
            .eq("account_id", self.account_id)
            .order("created_at")
            .execute()
        )
        return [self._row_to_rule(r) for r in response.data]

    # ── Jobs ───────────────────────────────────────────────────

    def create_job(self, dataset_id: str, total_count: int = 0) -> str:
        """Create a new enrichment job. Returns the job ID."""
        row = {
            "account_id": self.account_id,
            "dataset_id": dataset_id,
            "status": "running",
            "total_count": total_count,
        }
        response = self.client.table("jobs").insert(row).execute()
        return response.data[0]["id"]

    def update_job(self, job_id: str, **kwargs) -> None:
        """Update job fields (status, current_count, message, stats, etc.)."""
        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        (
            self.client.table("jobs")
            .update(kwargs)
            .eq("id", job_id)
            .eq("account_id", self.account_id)
            .execute()
        )

    def get_job(self, job_id: str) -> dict | None:
        """Get job status in the same format as the in-memory JOBS dict."""
        response = (
            self.client.table("jobs")
            .select("*")
            .eq("id", job_id)
            .eq("account_id", self.account_id)
            .execute()
        )
        if not response.data:
            return None
        row = response.data[0]
        return {
            "status": row["status"],
            "current": row.get("current_count", 0),
            "total": row.get("total_count", 0),
            "message": row.get("message", ""),
            "stats": row.get("stats") or {},
        }

    # ── Conversion helpers ─────────────────────────────────────

    def _profile_to_row(
        self, profile: Profile, dataset_id: str | None = None
    ) -> dict:
        row = {
            "id": profile.id,
            "account_id": self.account_id,
            "name": profile.name,
            "email": profile.email,
            "linkedin_url": profile.linkedin_url,
            "organization": profile.organization,
            "title": profile.title,
            "phone": profile.phone,
            "twitter_url": profile.twitter_url,
            "website_url": profile.website_url,
            "resume_url": profile.resume_url,
            "other_links": profile.other_links,
            "linkedin_enriched": profile.linkedin_enriched,
            "content_fields": profile.content_fields,
            "metadata": profile.metadata,
            "fetched_content": profile.fetched_content,
            "profile_card": profile.profile_card,
            "field_summaries": profile.field_summaries,
            "enrichment_status": profile.enrichment_status.value,
            "enrichment_log": profile.enrichment_log,
        }
        if dataset_id is not None:
            row["dataset_id"] = dataset_id
        return row

    @staticmethod
    def _parse_json(val, default=None):
        """Parse a JSONB value that may be returned as a string."""
        if default is None:
            default = {}
        if val is None:
            return default
        if isinstance(val, str):
            try:
                import json
                return json.loads(val)
            except (json.JSONDecodeError, ValueError):
                return default
        return val

    def _row_to_profile(self, row: dict) -> Profile:
        pj = self._parse_json
        return Profile(
            id=row["id"],
            name=row.get("name") or "",
            email=row.get("email") or "",
            linkedin_url=row.get("linkedin_url") or "",
            organization=row.get("organization") or "",
            title=row.get("title") or "",
            phone=row.get("phone") or "",
            twitter_url=row.get("twitter_url") or "",
            website_url=row.get("website_url") or "",
            resume_url=row.get("resume_url") or "",
            other_links=pj(row.get("other_links"), []),
            linkedin_enriched=pj(row.get("linkedin_enriched"), {}),
            content_fields=pj(row.get("content_fields"), {}),
            metadata=pj(row.get("metadata"), {}),
            fetched_content=pj(row.get("fetched_content"), {}),
            profile_card=row.get("profile_card") or "",
            field_summaries=pj(row.get("field_summaries"), {}),
            enrichment_status=EnrichmentStatus(
                row.get("enrichment_status", "pending")
            ),
            enrichment_log=pj(row.get("enrichment_log"), []),
        )

    def _search_to_row(self, search: DefinedSearch) -> dict:
        return {
            "id": search.id,
            "account_id": self.account_id,
            "name": search.name,
            "query": search.query,
            "clarification_context": search.clarification_context,
            "search_rules": search.search_rules,
            "exemplars": [e.model_dump(mode="json") for e in search.exemplars],
            "cache_prompt_hash": search.cache.prompt_hash,
            "cache_scores": {
                pid: sr.model_dump(mode="json")
                for pid, sr in search.cache.scores.items()
            },
            "applicable_global_rule_ids": search.applicable_global_rule_ids,
            "excluded_profile_ids": search.excluded_profile_ids,
            "prompt_corrections": search.prompt_corrections,
            "created_at": (
                search.created_at.isoformat()
                if isinstance(search.created_at, datetime)
                else str(search.created_at)
            ),
        }

    def _row_to_search(
        self, row: dict, feedback: list[FeedbackEvent] | None = None
    ) -> DefinedSearch:
        pj = self._parse_json
        cache_scores = {}
        for pid, sr in pj(row.get("cache_scores"), {}).items():
            cache_scores[pid] = ScoreResult(**sr)

        return DefinedSearch(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            clarification_context=row.get("clarification_context", ""),
            created_at=row.get("created_at", datetime.now(timezone.utc)),
            search_rules=pj(row.get("search_rules"), []),
            exemplars=[Exemplar(**e) for e in pj(row.get("exemplars"), [])],
            feedback_log=feedback or [],
            cache=SearchCache(
                prompt_hash=row.get("cache_prompt_hash", ""),
                scores=cache_scores,
            ),
            applicable_global_rule_ids=pj(row.get("applicable_global_rule_ids"), []),
            excluded_profile_ids=pj(row.get("excluded_profile_ids"), []),
            prompt_corrections=pj(row.get("prompt_corrections"), []),
        )

    def _feedback_to_row(self, search_id: str, event: FeedbackEvent) -> dict:
        return {
            "search_id": search_id,
            "account_id": self.account_id,
            "profile_id": event.profile_id,
            "profile_name": event.profile_name,
            "rating": event.rating,
            "reason": event.reason,
            "reasoning_correction": event.reasoning_correction,
            "scope": event.scope,
            "created_at": (
                event.timestamp.isoformat()
                if isinstance(event.timestamp, datetime)
                else str(event.timestamp)
            ),
        }

    def _row_to_feedback(self, row: dict) -> FeedbackEvent:
        return FeedbackEvent(
            profile_id=row.get("profile_id", ""),
            profile_name=row.get("profile_name", ""),
            rating=row.get("rating", "no"),
            reason=row.get("reason"),
            reasoning_correction=row.get("reasoning_correction"),
            scope=row.get("scope", "search"),
            timestamp=row.get("created_at", datetime.now(timezone.utc)),
        )

    def _rule_to_row(self, rule: GlobalRule) -> dict:
        return {
            "id": rule.id,
            "account_id": self.account_id,
            "text": rule.text,
            "scope": rule.scope,
            "source": rule.source,
            "created_at": (
                rule.created_at.isoformat()
                if isinstance(rule.created_at, datetime)
                else str(rule.created_at)
            ),
        }

    def _row_to_rule(self, row: dict) -> GlobalRule:
        return GlobalRule(
            id=row["id"],
            text=row["text"],
            scope=row.get("scope", "all searches"),
            source=row.get("source", "manual"),
            created_at=row.get("created_at", datetime.now(timezone.utc)),
        )
