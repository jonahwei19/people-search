"""Evaluation harness for the enrichment pipeline.

This package provides offline analysis tools that read existing profile
state (without re-enriching) so pipeline changes can be measured
before/after. See:

- `coverage_report` — coverage, cost, source contribution, log patterns
- `wrong_person_audit` — samples enriched profiles and flags mismatches
  between uploaded name and linkedin_enriched.full_name
- `replay` — parameterised offline replay of `_verify_match` against
  stored enrichment_log data; zero API cost
- `cohort_analysis` — slices hit-rate + cost + wrong-person rate along
  email type, org presence, name length, and crossed axes
- `cost_simulator` — price proposed pipeline specs against a population
  before running them
"""
