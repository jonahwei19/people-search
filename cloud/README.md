# Cloud Deployment

Vercel (serverless Python) + Supabase (Postgres + JSONB) deployment of People Search.

## Setup

### 1. Apply the schema

Paste `cloud/schema.sql` into the [Supabase SQL Editor](https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new) and run it. Schema includes RLS policies for all tables.

### 2. Apply outstanding migrations

Migrations live in `cloud/migrations/` and are append-only. Run them in order:

- `002_enrichment_version.sql` — adds `enrichment_version` column
- `003_add_enriched_identity_fields.sql` — adds `enriched_organization` / `enriched_title` shadow fields
- `004_verification_decisions.sql` — adds `verification_decisions JSONB` for structured verifier logs

### 3. Create an account

```sql
SELECT create_account('YourOrgName', 'your-password');
```

New accounts inherit API keys from Vercel env vars on first login (see `cloud/auth.py::seed_env_keys`).

### 4. Deploy

```bash
cd projects/candidate-search-tool
vercel login && vercel link
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_KEY
vercel env add SESSION_SECRET
# Platform-level keys (auto-seeded into new accounts):
vercel env add BRAVE_API_KEY
vercel env add SERPER_API_KEY
vercel env add ENRICHLAYER_API_KEY
vercel env add GOOGLE_API_KEY        # Gemini
vercel deploy
```

Auto-deploy from GitHub `main` branch is enabled.

## Architecture

```
candidate-search-tool/
├── vercel.json          # runtime config + /api/* → /api/* rewrites
├── requirements.txt
├── shared/ui.html       # source of truth for the frontend
├── build.sh             # syncs shared/ui.html → cloud/public/ + public/
├── cloud/
│   ├── schema.sql        # canonical schema (RLS + helper RPCs)
│   ├── migrations/       # append-only SQL migrations
│   ├── auth.py           # session cookies + per-account keys + env-seed
│   ├── storage/supabase.py   # storage adapter (account-scoped)
│   └── public/index.html     # bundled frontend
├── api/                 # Vercel Python routes
└── public/index.html    # served by Vercel
```

## API surface

### Auth
- `POST /api/auth_login` — name + password → signed cookie
- `POST /api/auth_logout`
- `GET /api/keys` — returns account's keys + `platform_defaults` array (which keys have env fallbacks)
- `POST /api/keys` — override platform defaults

### Datasets + enrichment
- `POST /api/detect_schema` — upload file, auto-detect columns
- `POST /api/prepare` — parse with confirmed mappings + cost estimate
- `POST /api/enrich` — **phased chunked** enrichment: `enriching` → `fetching` → `carding`. Frontend polls `pollJob` which re-invokes on `status=running`. Survives function restarts via `jobs.stats.phase`.
- `POST /api/embed_only` — skip enrichment, just build profile cards
- `POST /api/reenrich` — re-run on existing dataset (resets statuses)
- `POST /api/reenrich_estimate` — cost estimate before `/api/reenrich`
- `GET /api/datasets` — list
- `GET/DELETE /api/dataset/[id]` — detail / delete (CASCADE to profiles)
- `GET /api/profile/[id]`
- `GET /api/job/[id]` — poll progress
- `POST /api/job/[id]/cancel`

### Search
- `POST /api/search/score` — score profiles against query (up to 900s timeout)
- `POST /api/search/searches/[id]/rerun` — re-run after feedback synthesis
- `POST /api/search/searches/[id]/synthesize` — LLM proposes rule/exemplar updates
- `POST /api/search/searches/[id]/rename`
- `GET /api/search/searches/[id]` / `[id]/results`
- `POST /api/search/feedback` — thumbs-up/down + reason
- `POST /api/search/chat` — clarifying questions before first run
- `GET/POST /api/search/global_rules`

### Airtable
- `POST /api/airtable/connect` / `import` / `writeback`

## Key design decisions

- **Chunked enrichment**: `/api/enrich` processes 50 profiles per invocation during the `enriching` phase, 20 during `fetching`, then does `carding` in one pass. Phase tracked in `jobs.stats.phase` so the flow resumes across function restarts and frontend polls.
- **Rewrites**: `vercel.json` maps `/api/*` → `api/*` so API files sit at the project root but stay organized.
- **Auth**: HMAC-SHA256 signed session cookies, 7-day TTL. No external auth deps.
- **Storage**: service key bypasses RLS; the adapter filters by `account_id` in every query. RLS still protects direct anon-key access.
- **Per-account keys + platform fallback**: each account can override keys in Settings. Missing keys fall through to Vercel env vars. `seed_env_keys` copies env values into the account on login so new users work out of the box.
- **Scoring**: synchronous, up to 900s. Works for datasets under ~5K profiles.
- **Deep linking**: `#search/ID` hash routing; the frontend auto-navigates on page load (works across the login flow).
