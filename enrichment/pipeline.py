"""Enrichment pipeline orchestrator.

The full flow:
1. Parse uploaded file (CSV or JSON)
2. Detect schema → propose field mappings
3. Check for duplicates across existing datasets
4. Create profiles from rows using mappings
5. Estimate costs
6. Run enrichment: LinkedIn + link fetching
7. Build profile cards (summarized text for LLM judge)
8. Save dataset

Usage:
    pipeline = EnrichmentPipeline(data_dir="./datasets")

    # Step 1-2: Upload and detect schema
    mappings = pipeline.detect_schema("contacts.csv")

    # Step 3-5: Parse, dedup, estimate costs
    dataset, cost = pipeline.prepare("contacts.csv", mappings, name="Q1 Contacts")
    print(cost.summary())
    print(f"Duplicates found: {len(dataset.enrichment_stats.get('duplicates', []))}")

    # Step 6-7: Enrich, fetch links, summarize
    pipeline.run_enrichment(dataset)
    pipeline.build_profile_cards(dataset)

    # Step 8: Save
    pipeline.save(dataset)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable

from .models import Dataset, Profile, EnrichmentStatus
from .schema import SchemaDetector, FieldMapping, FieldType

# Bump when the enrichment pipeline changes in ways that affect output quality
# (different search strategy, different scoring, different identity heuristics).
#
# Known tags:
#   v0-legacy — pre-versioning era. Never set by this constant; used only by
#               migration 002 to back-fill rows produced before 2026-04-19,
#               when pipeline code was not version-stamped. Rows with this tag
#               should be treated as "needs re-enrichment under current code".
#   v1        — initial cloud pipeline: Brave/Serper search + EnrichLayer +
#               non-LinkedIn evidence salvage. Established 2026-04-19. This is
#               the tag written to profiles touched by the v1 run_enrichment
#               code path (strategy="v1").
#   v2        — source-agnostic Variant A+ pipeline (see enrichment/v2/).
#               Stages: cohort → org_site → verticals (OpenAlex, GitHub,
#               Substack) → LinkedIn → open-web fallback → two-anchor
#               verifier. Established 2026-04-19. Written to profiles
#               touched via strategy="v2" (default for fresh runs).
ENRICHMENT_VERSION = "v2"
from .costs import CostEstimator, CostBreakdown
from .enrichers import LinkedInEnricher, is_valid_linkedin_url
from .identity import IdentityResolver
from .dedup import find_duplicates, load_all_datasets
from .fetchers import fetch_all_links


class EnrichmentPipeline:
    """Orchestrates the full upload → enrich → embed flow."""

    def __init__(
        self,
        data_dir: str | Path = "./datasets",
        enrichlayer_api_key: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.schema_detector = SchemaDetector()
        self.cost_estimator = CostEstimator()
        self.enricher = LinkedInEnricher(api_key=enrichlayer_api_key)
        self.resolver = IdentityResolver()

    # ── Step 1-2: Schema detection ──────────────────────────

    def detect_schema(self, file_path: str | Path) -> list[FieldMapping]:
        """Detect schema from uploaded file."""
        path = Path(file_path)
        if path.suffix.lower() == ".json":
            return self.schema_detector.detect_json(path)
        else:
            return self.schema_detector.detect_csv(path)

    # ── Step 3-4: Parse + cost estimate ─────────────────────

    def prepare(
        self,
        file_path: str | Path,
        mappings: list[FieldMapping],
        name: str = "",
        existing_profiles: list[Profile] | None = None,
    ) -> tuple[Dataset, CostBreakdown]:
        """Parse file into profiles and estimate enrichment costs.

        Args:
            file_path: Path to CSV/JSON file
            mappings: Confirmed field mappings
            name: Dataset name
            existing_profiles: Already-enriched profiles to skip

        Returns:
            (dataset, cost_breakdown)
        """
        path = Path(file_path)
        rows = self._load_rows(path)

        # Build profiles from rows using mappings, filter junk rows
        profiles = []
        skipped_rows = 0
        for i, row in enumerate(rows):
            profile = self._row_to_profile(row, mappings, i)
            # Skip rows with no identity AND no content (junk/empty rows)
            has_identity = profile.name or profile.email or profile.linkedin_url
            has_content = any(v.strip() for v in profile.content_fields.values()) if profile.content_fields else False
            if not has_identity and not has_content:
                skipped_rows += 1
                continue
            profiles.append(profile)

        # Cross-dataset dedup: check new profiles against ALL existing datasets
        existing_datasets = load_all_datasets(self.data_dir)
        duplicates = []
        already_enriched = 0
        if existing_datasets:
            duplicates = find_duplicates(profiles, existing_datasets)
            # Mark definite duplicates (email/linkedin match) as already enriched
            for match in duplicates:
                if match.confidence >= 0.9:
                    p = profiles[match.new_profile_idx]
                    if p.enrichment_status == EnrichmentStatus.PENDING:
                        p.enrichment_status = EnrichmentStatus.ENRICHED
                        already_enriched += 1

        # Also dedup against explicitly provided existing profiles
        if existing_profiles:
            existing_keys = set()
            for p in existing_profiles:
                if p.linkedin_url:
                    existing_keys.add(p.linkedin_url.rstrip("/").lower())
                if p.email:
                    existing_keys.add(p.email.lower())
            for p in profiles:
                if p.enrichment_status != EnrichmentStatus.PENDING:
                    continue
                if p.linkedin_url and p.linkedin_url.rstrip("/").lower() in existing_keys:
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    already_enriched += 1
                elif p.email and p.email.lower() in existing_keys:
                    p.enrichment_status = EnrichmentStatus.ENRICHED
                    already_enriched += 1

        # Count what we have
        have_linkedin = sum(1 for p in profiles if is_valid_linkedin_url(p.linkedin_url))
        have_email_only = sum(
            1 for p in profiles
            if p.email and not is_valid_linkedin_url(p.linkedin_url)
        )

        # Estimate costs
        cost = self.cost_estimator.estimate(
            total_profiles=len(profiles),
            have_linkedin_url=have_linkedin,
            have_email_only=have_email_only,
            already_enriched=already_enriched,
        )

        # Build dataset
        dataset = Dataset(
            name=name or path.stem,
            source_file=str(path),
            total_rows=len(rows),
            profiles=profiles,
            field_mappings=[m.to_dict() for m in mappings],
        )
        dataset.enrichment_stats["skipped_rows"] = skipped_rows
        dataset.enrichment_stats["duplicates"] = [d.to_dict() for d in duplicates]

        return dataset, cost

    # ── Step 5a: Identity resolution (email → LinkedIn) ────

    def resolve_identities(
        self,
        dataset: Dataset,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Find LinkedIn URLs for profiles that only have name+email/org.

        Uses Brave Search to find LinkedIn profiles. Modifies profiles in place.
        """
        stats = self.resolver.resolve_batch(
            dataset.profiles, on_progress=on_progress,
        )
        self.save(dataset)
        return stats

    # ── Step 5b: LinkedIn enrichment ────────────────────────

    def run_enrichment(
        self,
        dataset: Dataset,
        on_progress: Callable[[int, int, str], None] | None = None,
        strategy: str = "v2",
    ) -> dict:
        """Full enrichment. Default strategy is v2 (Variant A+, source-agnostic).

        Args:
            dataset:    The dataset to enrich (modified in place).
            on_progress: Optional progress callback.
            strategy:   "v2" (default) or "v1" for the legacy LinkedIn-first
                        path. Existing datasets that opted into v1 can pass
                        strategy="v1" for parity re-runs.

        Returns:
            Aggregate stats dict including enrichment_version.
        """
        if strategy == "v1":
            return self._run_enrichment_v1(dataset, on_progress=on_progress)
        if strategy == "v2":
            return self._run_enrichment_v2(dataset, on_progress=on_progress)
        raise ValueError(f"unknown enrichment strategy: {strategy!r}")

    def _run_enrichment_v1(
        self,
        dataset: Dataset,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Legacy LinkedIn-first pipeline (v1 tag). Kept for back-compat."""
        # Step 1: Resolve emails → LinkedIn URLs
        resolve_stats = self.resolve_identities(dataset, on_progress=on_progress)

        # Step 2: Enrich LinkedIn profiles (including newly resolved ones)
        def on_batch_save():
            self.save(dataset)

        enrich_stats = self.enricher.enrich_batch(
            dataset.profiles,
            on_progress=on_progress,
            on_batch_save=on_batch_save,
        )

        for p in dataset.profiles:
            if p.enrichment_status in (EnrichmentStatus.ENRICHED, EnrichmentStatus.SKIPPED, EnrichmentStatus.FAILED):
                p.enrichment_version = "v1"

        stats = {
            "resolved": resolve_stats.get("resolved", 0),
            "resolve_failed": resolve_stats.get("failed", 0),
            "enriched": enrich_stats.get("enriched", 0),
            "skipped": enrich_stats.get("skipped", 0),
            "failed": enrich_stats.get("failed", 0),
            "total_cost": enrich_stats.get("total_cost", 0),
            "enrichment_version": "v1",
            "strategy": "v1",
        }
        dataset.enrichment_stats.update(stats)
        return stats

    def _run_enrichment_v2(
        self,
        dataset: Dataset,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Variant A+ source-agnostic pipeline (v2 tag)."""
        from .v2 import run_v2

        def on_batch_save():
            self.save(dataset)

        v2_stats = run_v2(
            dataset.profiles,
            on_progress=on_progress,
            on_batch_save=on_batch_save,
            enrichlayer_api_key=self.enricher.api_key,
        )

        # Stamp pipeline version onto every profile that was touched this run
        for p in dataset.profiles:
            if p.enrichment_status in (EnrichmentStatus.ENRICHED, EnrichmentStatus.SKIPPED, EnrichmentStatus.FAILED):
                p.enrichment_version = "v2"

        stats = {
            "enriched": v2_stats.get("enriched", 0),
            "thin": v2_stats.get("thin", 0),
            "hidden": v2_stats.get("hidden", 0),
            "failed": v2_stats.get("failed", 0),
            "total_cost": v2_stats.get("total_cost_usd", 0.0),
            "enrichment_version": "v2",
            "strategy": "v2",
        }
        dataset.enrichment_stats.update(stats)
        return stats

    # ── Step 6: Fetch link content ──────────────────────────

    def fetch_links(
        self,
        dataset: Dataset,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Fetch content from URLs (GitHub, websites, etc.)."""
        profiles_with_links = [
            p for p in dataset.profiles
            if p.twitter_url or p.website_url or p.resume_url or p.other_links
        ]

        stats = {"fetched": 0, "failed": 0, "skipped": 0}
        for i, p in enumerate(profiles_with_links):
            if on_progress:
                on_progress(i + 1, len(profiles_with_links), f"Fetching links for {p.display_name()}")

            results = fetch_all_links(
                twitter_url=p.twitter_url,
                website_url=p.website_url,
                resume_url=p.resume_url,
                other_links=p.other_links,
            )
            for name, result in results.items():
                if result.success:
                    p.fetched_content[name] = result.text
                    stats["fetched"] += 1
                elif "not yet implemented" in result.error:
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1

        return stats

    # ── Step 7: Build profile cards ─────────────────────────

    def build_profile_cards(
        self,
        dataset: Dataset,
        use_llm: bool = False,
    ) -> None:
        """Build compact profile cards for LLM scoring.

        Each profile gets a summarized raw_text that fits in the
        judge's token budget. Verbose self-reported fields get
        compressed, first-person observations are preserved.
        """
        fields_seen = set()
        for p in dataset.profiles:
            p.build_raw_text(use_llm=use_llm)
            fields_seen.update(p.searchable_text_fields().keys())

        dataset.searchable_fields = sorted(fields_seen)

    # ── Step 7: Save/Load ───────────────────────────────────

    def save(self, dataset: Dataset):
        """Save dataset to disk."""
        path = self.data_dir / f"{dataset.id}.json"
        dataset.save(path)

    def load(self, dataset_id: str) -> Dataset:
        """Load a dataset by ID."""
        path = self.data_dir / f"{dataset_id}.json"
        return Dataset.load(path)

    def list_datasets(self) -> list[dict]:
        """List all saved datasets (id, name, profile count)."""
        datasets = []
        for p in sorted(self.data_dir.glob("*.json")):
            if p.name.endswith("_embeddings.npz"):
                continue
            try:
                ds = Dataset.load(p)
                datasets.append({
                    "id": ds.id,
                    "name": ds.name,
                    "profiles": len(ds.profiles),
                    "created_at": ds.created_at,
                    "source_file": ds.source_file,
                    "searchable_fields": ds.searchable_fields,
                })
            except Exception:
                continue
        return datasets

    # ── Internal helpers ────────────────────────────────────

    def _load_rows(self, path: Path) -> list[dict]:
        """Load rows from CSV or JSON."""
        if path.suffix.lower() == ".json":
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else [data]
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                return list(reader)

    def _row_to_profile(
        self, row: dict, mappings: list[FieldMapping], row_index: int
    ) -> Profile:
        """Convert a data row to a Profile using field mappings."""
        profile = Profile(source_row=row_index)

        for mapping in mappings:
            value = row.get(mapping.source_column, "")
            if not value or not str(value).strip():
                continue
            value = str(value).strip()

            match mapping.field_type:
                case FieldType.IDENTITY_NAME:
                    # Multiple name fields (First Name + Last Name) → concatenate
                    # But skip if this value is already contained in the name
                    # (avoids "Nathan Leonard Nathan Leonard" from First+Last+Full Name)
                    if profile.name:
                        if value.lower() in profile.name.lower() or profile.name.lower() in value.lower():
                            # Duplicate or subset — keep the longer one
                            if len(value) > len(profile.name):
                                profile.name = value
                        else:
                            profile.name = f"{profile.name} {value}"
                    else:
                        profile.name = value
                case FieldType.IDENTITY_EMAIL:
                    profile.email = value
                case FieldType.IDENTITY_LINKEDIN:
                    profile.linkedin_url = value
                case FieldType.IDENTITY_ORG:
                    profile.organization = value
                case FieldType.IDENTITY_TITLE:
                    profile.title = value
                case FieldType.IDENTITY_PHONE:
                    profile.phone = value
                case FieldType.LINK_TWITTER:
                    profile.twitter_url = value
                case FieldType.LINK_WEBSITE:
                    profile.website_url = value
                case FieldType.LINK_RESUME:
                    profile.resume_url = value
                case FieldType.LINK_OTHER:
                    profile.other_links.append(value)
                case FieldType.LINKEDIN_TEXT:
                    # Pre-enriched LinkedIn text — import directly, skip EnrichLayer
                    profile.linkedin_enriched = {"context_block": value}
                    profile.enrichment_status = EnrichmentStatus.ENRICHED
                    profile.enrichment_log.append("LinkedIn imported from pre-enriched column")
                case FieldType.CONTENT:
                    profile.content_fields[mapping.target_name] = value
                case FieldType.METADATA:
                    profile.metadata[mapping.target_name] = value
                case FieldType.IGNORE:
                    pass

        return profile
