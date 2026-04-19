# People Search

Upload a list of people → automatic enrichment (LinkedIn + non-LinkedIn web data) → natural-language search with LLM-as-judge scoring. Multi-tenant, deployed on Vercel + Supabase.

## What it does

1. **Upload**: CSV or JSON of people. Schema auto-detected.
2. **Enrich**: for each person, find their LinkedIn, scrape their web footprint (org page, GitHub, Substack, academic papers, personal site, Twitter), verify identity with a two-anchor rule.
3. **Search**: define a natural-language query ("AI safety researchers with government experience"). Gemini scores every profile. Feedback from you refines the query over time.
4. **Outreach**: email drafts, export, CRM integration.

## Repo layout

```
candidate-search-tool/
├── api/                    # Vercel serverless Python routes
│   ├── enrich.py            # chunked enrichment (enriching → fetching → carding)
│   ├── reenrich.py          # re-run on existing dataset
│   ├── search/              # search endpoints (score, rerun, chat, feedback)
│   └── ...                  # auth, keys, datasets, profiles, jobs, airtable
├── cloud/
│   ├── schema.sql           # canonical Postgres schema (RLS policies included)
│   ├── auth.py              # HMAC-signed session cookies + per-account API keys
│   ├── storage/supabase.py  # Supabase adapter (all methods scoped by account_id)
│   ├── migrations/          # 002 (version stamp), 003 (shadow fields), 004 (decisions)
│   └── README.md            # deploy + architecture
├── enrichment/
│   ├── pipeline.py          # orchestrator — v1 by default, strategy="v2" opts into new
│   ├── identity.py          # name → LinkedIn URL resolution (Brave + Serper)
│   ├── enrichers.py         # EnrichLayer + _verify_match (slug-aware, tiered scoring)
│   ├── arbiter.py           # Gemini tie-breaker for ambiguous candidates
│   ├── fetchers.py          # GitHub, website, Twitter, Google Drive content
│   ├── summarizer.py        # profile_card builder (compact text for the judge)
│   ├── v2/                  # source-agnostic pipeline — see v2/__init__.py
│   │   ├── cohort.py        # email classifier (edu/gov/corp/personal)
│   │   ├── org_site.py      # crawl org domain team/people pages
│   │   ├── vertical_*.py    # OpenAlex, GitHub, Substack APIs
│   │   ├── linkedin_resolve.py  # wraps v1 identity+enrich
│   │   ├── open_web.py      # last-resort Brave/Serper
│   │   ├── verify.py        # two-anchor verifier
│   │   ├── evidence.py      # structured Evidence dataclass
│   │   └── orchestrator.py  # skip-when-satisfied stage flow
│   ├── eval/
│   │   ├── coverage_report.py       # status, cohort, source, cost breakdown
│   │   ├── wrong_person_audit.py    # scans for name-mismatch LinkedIn attributions
│   │   ├── replay.py                # re-score stored logs under different configs
│   │   ├── cohort_analysis.py       # cohort-specific hit rates + recommendations
│   │   ├── cost_simulator.py        # project cost of proposed pipeline spec
│   │   ├── groundtruth.py           # load hand-labeled CSV, compute P/R/F1
│   │   └── arbiter_ab.py            # A/B harness for arbiter runs
│   ├── _retry.py            # exponential backoff for transient network errors
│   ├── nicknames.py         # ~100 canonical/nickname pairs (Matt↔Matthew)
│   ├── models.py            # Profile, Dataset, EnrichmentStatus
│   ├── costs.py, dedup.py, schema.py, fetchers.py
│   ├── SETUP.md             # API key setup for each fetcher
│   └── README.md            # data model + flow
├── search/                 # scoring, chat/interrogation, feedback synthesis
├── shared/ui.html          # single-file frontend (bundled to cloud/public + public/)
├── tools/
│   ├── decontaminate_legacy_profiles.py   # clean up contaminated org/title fields
│   ├── export_groundtruth_sample.py       # stratified sample → hand-labeled CSV
│   └── aws/                               # EC2 agent deploy scripts
├── tests/                  # pytest suite — 185 tests, runs in ~1s
├── plans/                  # diagnosis reports, baselines, A/B results
│   ├── diagnosis_architecture.md  # Variant A+ design
│   ├── diagnosis_correctness.md   # FM1-FM6 audit
│   ├── diagnosis_hitrate.md       # hit-rate P1-P3 data
│   ├── cohort_analysis_*.md, arbiter_ab_test.md, baseline_*.txt
│   └── groundtruth_*.csv          # ready for hand-labeling
├── archive/                # older code + old spec docs (old_notes/, v1_legacy/, search_v1/)
├── vercel.json, requirements.txt, package.json
└── build.sh                # sync shared/ui.html → cloud/public/ + public/
```

## Quick start

### Local dev

```bash
pip install -r requirements.txt
# Set env vars in .env (see enrichment/SETUP.md)
python3 -m flask --app app run     # local Flask mirror of the cloud API
```

Or `vercel dev` for the serverless path.

### Tests

```bash
python3 -m pytest tests/          # 185 tests, ~1 second
```

### Deploy

See `cloud/README.md`.

## Key concepts

- **v1 vs v2 pipeline**: `pipeline.run_enrichment(dataset, strategy="v1")` is the default (LinkedIn-centric with improvements). `strategy="v2"` routes through `enrichment/v2/` (source-agnostic: org-site crawl → vertical APIs → LinkedIn → open-web, two-anchor verifier). Bump `ENRICHMENT_VERSION` when you ship a pipeline change that should invalidate old data.
- **Versioning**: every enriched profile carries `enrichment_version` (`v0-legacy`, `v1`, `v2`). Filter `WHERE enrichment_version = 'v0-legacy'` to find rows that need re-enrichment.
- **Shadow fields**: `enriched_organization` / `enriched_title` hold what LinkedIn said. User-uploaded `organization` / `title` are never overwritten (they're ground truth).
- **Verification decisions**: every `_verify_match` call appends to `profile.verification_decisions` — structured JSON with anchors matched, score, decision, reason.
- **Chunked enrichment**: `/api/enrich` processes in phases (`enriching` → `fetching` → `carding`) tracked in `jobs.stats.phase`. Frontend polls and re-invokes until done. Each chunk stays under Vercel's 60s limit.
- **Multi-tenant**: every table has `account_id`; RLS + the Python adapter filter all queries. Platform-level API keys in Vercel env vars are auto-seeded into new accounts on first login.

## Security

- RLS enabled on every table. Service key bypasses RLS; Python adapter filters by `account_id` in every query.
- When adding a new table to `cloud/schema.sql`, **always**:
  ```sql
  ALTER TABLE new_table ENABLE ROW LEVEL SECURITY;
  CREATE POLICY account_isolation ON new_table
    FOR ALL USING (account_id = current_setting('app.account_id', true))
    WITH CHECK (account_id = current_setting('app.account_id', true));
  ```
  then run both in the [Supabase SQL Editor](https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new).
- Verify via [Advisors > Security](https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/advisors/security).

## Deeper reading

- `cloud/README.md` — deployment + full API route index
- `enrichment/README.md` — data flow, Profile/Dataset models, v1/v2 strategy
- `enrichment/SETUP.md` — API keys for GitHub, EnrichLayer, Brave/Serper, Gemini
- `plans/diagnosis_architecture.md` — why the v2 redesign, stage-by-stage
- `plans/diagnosis_correctness.md` — the verification-bug audit (FM1–FM6)
- `plans/diagnosis_hitrate.md` — quantitative hit-rate analysis + ship order
