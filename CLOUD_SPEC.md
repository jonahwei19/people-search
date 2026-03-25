# People Search — Cloud Deployment Spec

## Overview

Move the local People Search app to a cloud-hosted version that multiple organizations can use. Each org gets an account (name + password), their own datasets, searches, and rules. No infrastructure management — Vercel + Supabase.

**Current state:** Flask app running locally, JSON files on disk, single user.
**Target state:** Vercel-hosted Next.js app, Supabase Postgres backend, multiple accounts, optional Airtable integration.

---

## Architecture

```
Browser
  │
  ▼
Vercel (Next.js)
  ├── Static frontend (React or plain HTML+JS — current UI works)
  ├── API routes (/api/*)
  │     ├── Short routes: schema detection, search scoring, dataset CRUD
  │     └── Background functions: enrichment pipeline (long-running)
  │
  ▼
Supabase (Postgres + Auth)
  ├── accounts table
  ├── datasets table
  ├── profiles table
  ├── searches table
  ├── feedback table
  ├── global_rules table
  └── jobs table (enrichment progress tracking)
```

External APIs (called from Vercel API routes):
- Brave Search (identity resolution)
- Serper/Google (identity resolution)
- EnrichLayer (LinkedIn enrichment)
- Gemini (search scoring, questioning, feedback synthesis)

---

## Workstream 1: Supabase Schema + Data Layer

### Goal
Replace JSON file storage with Supabase Postgres. All existing pipeline code should work by swapping the storage layer.

### Schema

```sql
-- Accounts (simple: name + password hash)
CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT UNIQUE NOT NULL,          -- "BlueDot", "IFP"
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  settings JSONB DEFAULT '{}'::jsonb  -- API keys, preferences
);

-- Datasets (one per upload)
CREATE TABLE datasets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  source_file TEXT,                    -- original filename
  total_rows INT DEFAULT 0,
  field_mappings JSONB DEFAULT '[]'::jsonb,
  searchable_fields JSONB DEFAULT '[]'::jsonb,
  enrichment_stats JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Profiles (one per person per dataset)
CREATE TABLE profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id UUID REFERENCES datasets(id) ON DELETE CASCADE,
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,

  -- Identity
  name TEXT,
  email TEXT,
  linkedin_url TEXT,
  organization TEXT,
  title TEXT,
  phone TEXT,

  -- Links
  twitter_url TEXT,
  website_url TEXT,
  resume_url TEXT,
  other_links JSONB DEFAULT '[]'::jsonb,

  -- Enriched data
  linkedin_enriched JSONB DEFAULT '{}'::jsonb,
  content_fields JSONB DEFAULT '{}'::jsonb,    -- arbitrary text fields
  metadata JSONB DEFAULT '{}'::jsonb,          -- structured fields
  fetched_content JSONB DEFAULT '{}'::jsonb,   -- content from links

  -- Profile card (what the LLM reads)
  profile_card TEXT DEFAULT '',
  field_summaries JSONB DEFAULT '{}'::jsonb,

  -- Pipeline state
  enrichment_status TEXT DEFAULT 'pending',    -- pending/enriched/failed/skipped
  enrichment_log JSONB DEFAULT '[]'::jsonb,

  created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes for common queries
CREATE INDEX idx_profiles_dataset ON profiles(dataset_id);
CREATE INDEX idx_profiles_account ON profiles(account_id);
CREATE INDEX idx_profiles_email ON profiles(email);
CREATE INDEX idx_profiles_linkedin ON profiles(linkedin_url);
CREATE INDEX idx_profiles_status ON profiles(enrichment_status);

-- Saved searches
CREATE TABLE searches (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  clarification_context TEXT DEFAULT '',
  search_rules JSONB DEFAULT '[]'::jsonb,
  exemplars JSONB DEFAULT '[]'::jsonb,
  cache_prompt_hash TEXT DEFAULT '',
  cache_scores JSONB DEFAULT '{}'::jsonb,     -- {profile_id: {score, reasoning}}
  applicable_global_rule_ids JSONB DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Feedback events
CREATE TABLE feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  search_id UUID REFERENCES searches(id) ON DELETE CASCADE,
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id UUID,
  profile_name TEXT,
  rating TEXT,                                 -- strong_yes/yes/no/strong_no
  reason TEXT,
  reasoning_correction TEXT,
  scope TEXT DEFAULT 'search',                 -- search/global
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Global rules (per account)
CREATE TABLE global_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  scope TEXT DEFAULT 'all searches',
  source TEXT DEFAULT 'manual',
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Enrichment jobs (replaces in-memory JOBS dict)
CREATE TABLE jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  dataset_id UUID REFERENCES datasets(id) ON DELETE CASCADE,
  status TEXT DEFAULT 'running',               -- running/embedding/done/error/cancelled
  current_count INT DEFAULT 0,
  total_count INT DEFAULT 0,
  message TEXT DEFAULT '',
  stats JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Row-level security: each account only sees its own data
ALTER TABLE datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE searches ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE global_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

-- RLS policies (using account_id from JWT or session)
CREATE POLICY account_isolation ON datasets
  USING (account_id = current_setting('app.account_id')::uuid);
CREATE POLICY account_isolation ON profiles
  USING (account_id = current_setting('app.account_id')::uuid);
CREATE POLICY account_isolation ON searches
  USING (account_id = current_setting('app.account_id')::uuid);
CREATE POLICY account_isolation ON feedback
  USING (account_id = current_setting('app.account_id')::uuid);
CREATE POLICY account_isolation ON global_rules
  USING (account_id = current_setting('app.account_id')::uuid);
CREATE POLICY account_isolation ON jobs
  USING (account_id = current_setting('app.account_id')::uuid);
```

### Storage adapter

Create `storage/supabase.py` that implements the same interface as the current `EnrichmentPipeline` save/load methods but uses Supabase:

```python
class SupabaseStorage:
    """Drop-in replacement for file-based storage."""

    def __init__(self, supabase_url: str, supabase_key: str, account_id: str):
        ...

    # Dataset operations
    def save_dataset(self, dataset: Dataset) -> None: ...
    def load_dataset(self, dataset_id: str) -> Dataset: ...
    def list_datasets(self) -> list[dict]: ...
    def delete_dataset(self, dataset_id: str) -> None: ...

    # Profile operations (bulk for efficiency)
    def save_profiles(self, dataset_id: str, profiles: list[Profile]) -> None: ...
    def load_profiles(self, dataset_id: str) -> list[Profile]: ...
    def update_profile(self, profile: Profile) -> None: ...

    # Search operations
    def save_search(self, search: DefinedSearch) -> None: ...
    def load_search(self, search_id: str) -> DefinedSearch: ...
    def list_searches(self) -> list[dict]: ...

    # Feedback
    def add_feedback(self, search_id: str, event: FeedbackEvent) -> None: ...
    def get_feedback(self, search_id: str) -> list[FeedbackEvent]: ...

    # Global rules
    def save_rules(self, rules: list[GlobalRule]) -> None: ...
    def load_rules(self) -> list[GlobalRule]: ...

    # Jobs
    def create_job(self, dataset_id: str) -> str: ...
    def update_job(self, job_id: str, **kwargs) -> None: ...
    def get_job(self, job_id: str) -> dict: ...
```

### Verification
- [ ] Can create an account in Supabase
- [ ] Can save/load a dataset with 100 profiles
- [ ] Can save/load searches with scores
- [ ] Can save/load feedback events
- [ ] RLS works: account A cannot see account B's data
- [ ] Profile dedup works across datasets (same account)
- [ ] Job status tracking works across multiple API route invocations
- [ ] Bulk profile insert handles 500+ rows efficiently (<5s)
- [ ] All existing pipeline tests pass with SupabaseStorage swapped in

---

## Workstream 2: Vercel Deployment

### Goal
Deploy the app to Vercel. The frontend is the current HTML/JS (inline in upload_web.py). API routes replicate the current Flask endpoints.

### Structure

```
people-search-cloud/
├── package.json
├── vercel.json
├── next.config.js
├── public/
│   └── (static assets if any)
├── pages/
│   ├── index.js              -- serves the HTML (can be the current inline template)
│   └── api/
│       ├── auth/
│       │   ├── login.js      -- POST: name + password → session cookie
│       │   └── logout.js
│       ├── datasets/
│       │   ├── index.js      -- GET: list, POST: create
│       │   └── [id].js       -- GET: detail, DELETE: delete
│       ├── detect-schema.js   -- POST: upload file, detect schema
│       ├── prepare.js         -- POST: parse with mappings, cost estimate
│       ├── enrich.js          -- POST: start enrichment (background function)
│       ├── reenrich.js        -- POST: re-enrich existing dataset
│       ├── job/[id].js        -- GET: job status
│       ├── profile/[id].js   -- GET: single profile
│       ├── keys.js            -- GET/POST: API keys (per account)
│       └── search/
│           ├── searches.js    -- GET: list, POST: create
│           ├── [id].js        -- GET: detail
│           ├── [id]/results.js
│           ├── [id]/synthesize.js
│           ├── [id]/rerun.js
│           ├── score.js       -- POST: start scoring
│           ├── progress/[id].js
│           ├── feedback.js    -- POST: submit feedback
│           ├── chat.js        -- POST: questioning
│           └── global-rules.js
├── lib/
│   ├── supabase.js            -- Supabase client init
│   ├── auth.js                -- session/cookie helpers
│   └── storage.js             -- SupabaseStorage (JS port or Python API call)
└── enrichment/                -- Python enrichment code (called via Vercel Python runtime)
    ├── (all existing Python files)
    └── ...
```

### Key decisions

**Python vs JavaScript for API routes:**

Option A: **Python runtime on Vercel** — API routes written in Python, reuse existing enrichment code directly. Vercel supports Python functions via `api/*.py` files. Simpler, less rewriting.

Option B: **Next.js API routes (JS)** — frontend and API both in JS. Enrichment pipeline would need to be either: (a) ported to JS, or (b) called as a separate Python microservice. More work.

**Recommendation: Option A.** Keep the enrichment code in Python, use Vercel's Python runtime for API routes. The frontend HTML/JS doesn't need Next.js — it can be served as a static file with Python API routes.

```
vercel.json:
{
  "functions": {
    "api/*.py": {
      "runtime": "@vercel/python@4",
      "maxDuration": 60
    },
    "api/enrich.py": {
      "runtime": "@vercel/python@4",
      "maxDuration": 900
    }
  }
}
```

**Background enrichment:**

Vercel Pro has 15-minute function timeout. Enrichment for 500 profiles takes ~10 minutes with parallel search. Options:

1. **Single long function** (Vercel Pro, 15 min timeout) — simplest
2. **Chunked**: API route processes 10 profiles, stores progress in Supabase, frontend polls and re-triggers — works on free tier
3. **External worker**: push to a queue, process elsewhere — overkill for now

**Recommendation: Option 2 (chunked).** Works on all Vercel tiers. The frontend polls `/api/job/[id]`, and if the function times out mid-batch, the next poll re-triggers processing from where it left off. Progress is in Supabase, not in-memory.

### File uploads

Current: Flask receives multipart upload, saves to `uploads/` directory.
Vercel: Serverless functions can receive uploads up to 4.5MB. For larger CSVs, use Supabase Storage (S3-compatible).

Flow:
1. Frontend uploads CSV to Supabase Storage bucket
2. API route reads from bucket, processes, deletes temp file

### Verification
- [ ] App deploys to Vercel and loads the UI
- [ ] Can upload a CSV and see schema detection results
- [ ] Can run enrichment and see progress updates
- [ ] Enrichment survives function restarts (progress in Supabase)
- [ ] Can run a search and see scored results
- [ ] Can submit feedback
- [ ] API keys stored per-account in Supabase (not in .env)
- [ ] Works on Vercel free tier (with chunked enrichment)

---

## Workstream 3: Account System

### Goal
Simple multi-tenant auth. No OAuth, no email verification. Just: account name + password → session cookie.

### Flow

1. First visit → login page: "Account name" + "Password" fields
2. On login → set HTTP-only session cookie with account_id
3. All API routes check cookie, inject account_id into Supabase queries
4. Logout clears cookie

### Account creation

For now: manual. Admin creates accounts by inserting into Supabase:
```sql
INSERT INTO accounts (name, password_hash) VALUES ('BlueDot', crypt('password123', gen_salt('bf')));
```

Later: self-service signup page if needed.

### API key storage

Each account stores its own API keys (Brave, Serper, EnrichLayer, Gemini) in the `accounts.settings` JSONB field. The Settings page reads/writes these per account.

```json
{
  "BRAVE_API_KEY": "...",
  "SERPER_API_KEY": "...",
  "ENRICHLAYER_API_KEY": "...",
  "GOOGLE_API_KEY": "..."
}
```

API routes read keys from the account's settings, not from environment variables.

### Verification
- [ ] Can log in with account name + password
- [ ] Session cookie persists across page reloads
- [ ] Account A cannot see account B's datasets, searches, or rules
- [ ] API keys are per-account and work for enrichment/search
- [ ] Can log out and log in to a different account
- [ ] Invalid password shows error message
- [ ] No auth required for the login page itself

---

## Workstream 4: Airtable Integration (Optional)

### Goal
Users can connect their Airtable base, import profiles from it, and optionally write enriched data back.

### Flow

1. User provides: Airtable API key + Base ID + Table name (or URL)
2. App reads all records from the table
3. Schema detection runs on the Airtable columns (same as CSV)
4. User confirms mappings
5. Profiles created in Supabase from Airtable data
6. Enrichment runs as normal
7. **Write-back** (optional): enriched fields written as new columns in the original Airtable table

### Write-back columns added to Airtable
- `PS_LinkedIn_URL` — resolved LinkedIn URL
- `PS_LinkedIn_Enriched` — enriched profile text (truncated to Airtable's field limit)
- `PS_Profile_Card` — compact profile card for search
- `PS_Enrichment_Status` — pending/enriched/failed/skipped
- `PS_Last_Enriched` — timestamp

### Sync
- One-way import initially (Airtable → People Search)
- Write-back is a separate explicit action ("Push enrichment back to Airtable")
- No continuous sync — user triggers import/write-back manually

### API

```
POST /api/airtable/connect
  Body: { api_key, base_id, table_name }
  Returns: { columns: [...], row_count: N, sample_rows: [...] }

POST /api/airtable/import
  Body: { api_key, base_id, table_name, mappings, dataset_name }
  Returns: { dataset_id }

POST /api/airtable/writeback
  Body: { dataset_id, api_key, base_id, table_name }
  Returns: { updated: N, failed: N }
```

### Verification
- [ ] Can connect to an Airtable base and see columns + sample data
- [ ] Schema detection works on Airtable columns
- [ ] Can import 500 records from Airtable into a dataset
- [ ] Enrichment runs on imported data
- [ ] Can write enrichment results back to Airtable as new columns
- [ ] Write-back doesn't overwrite existing Airtable data
- [ ] Handles Airtable API rate limits (5 req/sec)
- [ ] Handles Airtable field name conflicts (PS_ prefix avoids collisions)

---

## Workstream 5: Frontend Polish

### Goal
Make the UI work well as a multi-tenant cloud app. The current inline HTML is functional but needs a few additions.

### Changes from local version

1. **Login page** — account name + password form
2. **Account indicator** — show current account name in sidebar
3. **API keys in Settings** — per-account, stored in Supabase (not .env)
4. **Airtable connection UI** — in Settings: connect base, choose table
5. **Remove local-only messaging** — "runs entirely on your machine" → "your data is stored securely" (or similar)
6. **Loading states** — serverless cold starts can take 2-3s, show loading indicators
7. **Error handling** — network errors, session expiry, Supabase errors

### What stays the same
- Upload flow (schema detection → cost estimate → enrich)
- Dataset management (list, detail, delete, re-enrich)
- Search UI (query, scoring, results, feedback, rules)
- Profile modal (click to view full details)
- Settings page (API keys, privacy info)

### Verification
- [ ] Login page works, shows error on wrong password
- [ ] All existing UI flows work (upload, search, feedback)
- [ ] Account name visible in sidebar
- [ ] Settings page saves API keys to Supabase
- [ ] Airtable connection UI works (if Workstream 4 is done)
- [ ] Cold start: UI shows loading state, doesn't break
- [ ] Session expiry: redirects to login, doesn't lose work

---

## Dependency Order

```
Workstream 1 (Supabase schema)
    │
    ├──→ Workstream 2 (Vercel deployment)
    │         │
    │         └──→ Workstream 5 (Frontend polish)
    │
    └──→ Workstream 3 (Account system)

Workstream 4 (Airtable) — independent, can start anytime after Workstream 1
```

Workstreams 1, 3, and 4 can run in parallel once the schema is agreed on.

---

## Environment Setup

### Supabase
```bash
# Create project at supabase.com (free tier)
# Get: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY
# Run the SQL schema above in the SQL editor
```

### Vercel
```bash
npm i -g vercel
vercel login
vercel link  # connect to project
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_KEY
vercel deploy
```

### Local development
```bash
# Install Supabase CLI for local dev
brew install supabase/tap/supabase
supabase start  # local Postgres + auth

# Run the app locally
python3 upload_web.py  # existing Flask app, pointed at local Supabase
```

---

## What NOT to change

- The enrichment pipeline code (`enrichment/`) — works as-is, just swap storage
- The search scoring code (`search/`) — works as-is
- The identity resolution logic — works as-is
- The profile card builder — works as-is
- The schema detector — works as-is

The cloud version is a **deployment and storage change**, not a rewrite. The core pipeline is proven and should be preserved.

---

## Cost Estimate

| Service | Free tier | Expected cost |
|---------|-----------|---------------|
| Vercel | 100GB bandwidth, serverless functions | Free for this usage |
| Supabase | 500MB DB, 50K auth requests | Free for this usage |
| Brave Search | 2,000 queries/mo free | ~$5/mo if heavy use |
| Serper | 2,500 queries/mo free | ~$5/mo if heavy use |
| EnrichLayer | Pay per profile ($0.10) | Usage-based |
| Gemini | Free tier available | ~$2/mo for search scoring |

Total hosting cost: **$0/mo** on free tiers. API costs are usage-based and borne by each account (their own API keys).
