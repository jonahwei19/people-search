"""Evaluation harness for the enrichment pipeline.

This package provides offline analysis tools that read existing profile
state (without re-enriching) so pipeline changes can be measured
before/after. See:

- `coverage_report` — coverage, cost, source contribution, log patterns
- `wrong_person_audit` — samples enriched profiles and flags mismatches
  between uploaded name and linkedin_enriched.full_name
"""
