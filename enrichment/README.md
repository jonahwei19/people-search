# Enrichment Pipeline

Turn `(name, email, org?)` into a searchable profile with verified LinkedIn data plus non-LinkedIn web footprint (personal site, GitHub, Substack, academic pages, Twitter).

## Two strategies

### v1 (default) — LinkedIn-first with tightened verification

```
name + email + org
    │
    ▼
identity.py
    │  Brave + Serper search → LinkedIn URL candidates
    │  Short-circuits Serper when Brave returns ≥3 results
    │  Non-LinkedIn URLs collected as evidence_urls (verified with two-anchor rule)
    ▼
enrichers.py
    │  EnrichLayer API → full LinkedIn data
    │  _verify_match: tiered name scoring (strong/normal/weak) + org + location + slug
    │  Rejects: weak-match-without-corroboration, last-name-missing, org-mismatch-no-signal
    │  API errors keep status PENDING for retry; only real mismatches go to FAILED
    │  If verifier picks tied candidates (≤1pt apart), calls arbiter.py (Gemini)
    ▼
fetchers.py
    │  GitHub, website, Twitter (via Brave), Google Drive → fetched_content
    ▼
summarizer.py
    │  Build compact profile_card for LLM-judge scoring
```

### v2 — source-agnostic (in `enrichment/v2/`)

For cohorts where LinkedIn is weak (academics, policy, early-career). Cohort-aware stage ladder with skip-when-satisfied:

```
Stage 1: cohort.py         — classify email (gov/edu/corp/personal), derive org_domain
Stage 2: org_site.py       — crawl <org_domain>/{team,people,staff,bio,…}
Stage 3: vertical_*.py     — OpenAlex (edu), GitHub (tech), Substack (writers)
Stage 4: linkedin_resolve.py  — wraps v1 identity+enrich
Stage 5: open_web.py       — Brave/Serper fallback when stages 2–4 produce <2 anchors
Stage 6: verify.py         — two-anchor rule: an Evidence is STRONG only with ≥2
                             independent anchors (exact_email_on_page, name_tokens,
                             org_domain_match, title_match, affiliation_match,
                             url_slug_match, cross_link)
```

Enable with `pipeline.run_enrichment(dataset, strategy="v2")`. Default is still v1.

## Data model (`models.py`)

```python
@dataclass
class Profile:
    id: str
    # Identity (user upload is ground truth; never overwritten by enrichment)
    name: str; email: str; linkedin_url: str
    organization: str; title: str; phone: str
    # Links
    twitter_url: str; website_url: str; resume_url: str
    other_links: list[str]
    # Enriched LinkedIn data
    linkedin_enriched: dict
    # Shadow fields — LinkedIn-sourced values, kept separate from user upload
    enriched_organization: str
    enriched_title: str
    # Arbitrary content from upload
    content_fields: dict[str, str]
    metadata: dict
    # Content we fetched from non-LinkedIn URLs
    fetched_content: dict[str, str]
    # What the LLM judge reads
    profile_card: str
    field_summaries: dict[str, str]
    # Pipeline state
    enrichment_status: EnrichmentStatus  # pending / enriched / failed / skipped
    enrichment_log: list[str]
    enrichment_version: str   # "" | "v0-legacy" | "v1" | "v2"
    # Every verify_match decision — structured, auditable
    verification_decisions: list[dict]   # [{linkedin_url, score, anchors_positive,
                                         #   anchors_negative, decision, reason, ...}]
```

## File structure

```
enrichment/
├── pipeline.py          # run_enrichment(dataset, strategy="v1"|"v2")
├── identity.py          # Brave+Serper search, tied-candidate arbitration
├── enrichers.py         # EnrichLayer + _verify_match (tiered, slug-aware)
├── arbiter.py           # Gemini-as-judge for ambiguous candidate ties
├── fetchers.py          # GitHub, website, Twitter, Google Drive content
├── summarizer.py        # profile_card builder
├── dedup.py             # cross-dataset duplicate detection
├── costs.py             # cost estimation
├── schema.py            # schema auto-detection for uploads
├── models.py
├── _retry.py            # exponential backoff helper for transient errors
├── nicknames.py         # ~100 canonical/nickname pairs
├── v2/                  # source-agnostic pipeline (see Two strategies above)
├── eval/
│   ├── coverage_report.py      # CLI: status / cohort / source / cost breakdown
│   ├── wrong_person_audit.py   # CLI: scan enriched profiles for name-mismatches
│   ├── replay.py               # re-score stored logs under modified configs
│   ├── cohort_analysis.py      # per-cohort hit rates + recommendations
│   ├── cost_simulator.py       # project cost of a proposed pipeline spec
│   ├── groundtruth.py          # load hand-labeled CSV, compute P/R/F1
│   └── arbiter_ab.py           # A/B harness for the arbiter
├── README.md            # this file
├── SETUP.md             # API key setup for each fetcher
└── INTAKE_SPEC.md       # schema-detection design spec
```

## Eval harness

### Coverage report

```bash
python3 -m enrichment.eval.coverage_report \
  --account-id <id> --dataset-id <id>
```

Outputs: status distribution, cohort breakdowns (email × org), source contribution (linkedin / website / twitter / fetched_content), `enrichment_version` distribution, cost (queries + LinkedIn calls), log-pattern counts.

### Wrong-person audit

```bash
python3 -m enrichment.eval.wrong_person_audit \
  --account-id <id> --dataset-id <id>
```

Flags enriched profiles whose name doesn't match the LinkedIn full name. Uses `nicknames.py` to suppress Matt↔Matthew-style false positives.

### Offline replay

```python
from enrichment.eval.replay import ReplayConfig, replay_dataset

result = replay_dataset(profiles, ReplayConfig(
    name_strong_score=3, org_mismatch_penalty=-1, require_anchors=1
))
# → {"would_accept": N, "would_reject": M, "flips": [...]}
```

No API calls. Lets you test "what if we tightened the threshold?" against stored logs.

### Ground truth

```bash
# Export stratified sample for hand-labeling
python3 -m tools.export_groundtruth_sample \
  --account-id <id> --dataset-id <id> --n 50 \
  --out plans/groundtruth_<dataset>.csv

# After filling in the true_* columns by hand, score against it:
python3 -m enrichment.eval.coverage_report \
  --account-id <id> --dataset-id <id> \
  --groundtruth plans/groundtruth_<dataset>.csv
```

## Decontamination

Legacy rows enriched before the verification fixes shipped may have
LinkedIn-sourced junk in `organization` / `title`:

```bash
python3 -m tools.decontaminate_legacy_profiles \
  --account-id <id> [--dataset-id <id>] [--dry-run]
```

- Populates `enriched_organization` / `enriched_title` shadow fields
- Blanks user-facing `organization` / `title` when they equal the LinkedIn value and the audit flags the match as wrong-person

## Environment variables

See `enrichment/SETUP.md` for details on each.

```
BRAVE_API_KEY          # identity resolution (Brave Search)
SERPER_API_KEY         # identity resolution fallback (Google via Serper)
ENRICHLAYER_API_KEY    # LinkedIn profile enrichment
GOOGLE_API_KEY         # Gemini (summarizer, arbiter, LLM judge)
```

All are optional at the account level — platform-level defaults in Vercel env vars are auto-seeded into accounts on first login (`cloud/auth.py::seed_env_keys`).
