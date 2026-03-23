"""Enrichment pipeline for people search.

Upload any CSV/JSON of people → detect schema → estimate costs → enrich → build profile cards → search-ready.
"""

from .pipeline import EnrichmentPipeline
from .schema import SchemaDetector, FieldMapping, FieldType
from .costs import CostEstimator
from .models import Dataset, Profile
