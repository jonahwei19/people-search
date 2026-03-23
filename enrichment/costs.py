"""Cost estimation for enrichment operations.

Shows the user what enrichment will cost before running it.
Enrichment providers and their per-call costs:
- EnrichLayer LinkedIn: $0.10 per profile
- Identity resolution (web search): ~$0.005 per lookup (Brave API, ~5 queries each)
- Profile card generation: free (local)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostBreakdown:
    """Itemized cost estimate for an enrichment run."""
    linkedin_enrichments: int = 0
    linkedin_cost_per: float = 0.10
    identity_lookups: int = 0
    identity_cost_per: float = 0.005
    embedding_profiles: int = 0
    # Embeddings are free (local), but note the time cost

    @property
    def linkedin_total(self) -> float:
        return self.linkedin_enrichments * self.linkedin_cost_per

    @property
    def identity_total(self) -> float:
        return self.identity_lookups * self.identity_cost_per

    @property
    def total_cost(self) -> float:
        return self.linkedin_total + self.identity_total

    def summary(self) -> str:
        """Human-readable cost summary."""
        lines = []
        lines.append(f"Enrichment cost estimate:")
        lines.append(f"{'─' * 45}")

        if self.linkedin_enrichments:
            lines.append(
                f"  LinkedIn enrichment: {self.linkedin_enrichments:,} profiles "
                f"× ${self.linkedin_cost_per:.3f} = ${self.linkedin_total:.2f}"
            )
        if self.identity_lookups:
            lines.append(
                f"  Identity resolution: {self.identity_lookups:,} lookups "
                f"× ${self.identity_cost_per:.4f} = ${self.identity_total:.2f}"
            )
        if self.embedding_profiles:
            lines.append(
                f"  Embedding generation: {self.embedding_profiles:,} profiles (free, local)")

        lines.append(f"{'─' * 45}")
        lines.append(f"  Total estimated cost: ${self.total_cost:.2f}")

        if self.total_cost > 10:
            lines.append(f"  ⚠ This is a significant cost. Consider enriching a sample first.")
        if self.total_cost > 100:
            lines.append(f"  ⚠ Large batch. Strongly recommend testing with 50-100 profiles first.")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "linkedin_enrichments": self.linkedin_enrichments,
            "linkedin_cost_per": self.linkedin_cost_per,
            "linkedin_total": self.linkedin_total,
            "identity_lookups": self.identity_lookups,
            "identity_cost_per": self.identity_cost_per,
            "identity_total": self.identity_total,
            "embedding_profiles": self.embedding_profiles,
            "total_cost": self.total_cost,
        }


class CostEstimator:
    """Estimates costs for an enrichment run before it starts."""

    def __init__(
        self,
        linkedin_cost: float = 0.01,
        identity_cost: float = 0.005,
    ):
        self.linkedin_cost = linkedin_cost
        self.identity_cost = identity_cost

    def estimate(
        self,
        total_profiles: int,
        have_linkedin_url: int,
        have_email_only: int,
        already_enriched: int = 0,
    ) -> CostBreakdown:
        """Estimate costs based on what data we have.

        Args:
            total_profiles: Total number of profiles in the upload
            have_linkedin_url: Profiles that already have a LinkedIn URL
            have_email_only: Profiles with email but no LinkedIn URL
            already_enriched: Profiles already in our database (skip enrichment)
        """
        # LinkedIn enrichment: profiles with URLs minus already-enriched
        linkedin_to_enrich = max(0, have_linkedin_url - already_enriched)

        # Identity resolution: profiles with email but no LinkedIn URL
        # We'll try to find their LinkedIn via search
        identity_to_resolve = have_email_only

        return CostBreakdown(
            linkedin_enrichments=linkedin_to_enrich,
            linkedin_cost_per=self.linkedin_cost,
            identity_lookups=identity_to_resolve,
            identity_cost_per=self.identity_cost,
            embedding_profiles=total_profiles,
        )
