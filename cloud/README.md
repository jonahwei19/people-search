# Cloud Deployment

Vercel + Supabase deployment of People Search. See [CLOUD_SPEC.md](../CLOUD_SPEC.md) for the full spec.

## Setup

### 1. Apply the schema

Paste `schema.sql` into the Supabase SQL Editor and run it.

### 2. Create an account

```sql
SELECT create_account('YourOrgName', 'your-password');
```

### 3. Deploy to Vercel

```bash
cd projects/candidate-search-tool
vercel login
vercel link
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_KEY
vercel env add SESSION_SECRET
vercel deploy
```

### 4. Local development

```bash
pip install -r requirements.txt
# Set env vars, then run with vercel dev or use local Flask app
```

## What's implemented

| Workstream | Status |
|---|---|
| WS1: Supabase schema + data layer | Done |
| WS2: Vercel deployment | Done |
| WS3: Account system | Done |
| WS4: Airtable integration | Done |
| WS5: Frontend polish | Not started |

## Architecture

```
candidate-search-tool/          # Vercel project root
├── vercel.json                 # Runtime config + rewrites
├── requirements.txt            # Python dependencies
├── public/
│   └── index.html              # Frontend (extracted from Flask app)
├── cloud/
│   ├── schema.sql              # Postgres migration
│   ├── auth.py                 # Session cookies (HMAC-SHA256)
│   ├── storage/
│   │   └── supabase.py         # Storage adapter (18 methods)
│   └── api/                    # 30 Vercel Python API routes
│       ├── _helpers.py          # Shared utilities
│       ├── auth_login.py        # POST /api/auth_login
│       ├── auth_logout.py       # POST /api/auth_logout
│       ├── keys.py              # GET/POST /api/keys
│       ├── detect_schema.py     # POST /api/detect_schema
│       ├── prepare.py           # POST /api/prepare
│       ├── enrich.py            # POST /api/enrich (chunked)
│       ├── embed_only.py        # POST /api/embed_only
│       ├── reenrich.py          # POST /api/reenrich (chunked)
│       ├── reenrich_estimate.py # POST /api/reenrich_estimate
│       ├── datasets.py          # GET /api/datasets
│       ├── dataset/[id].py      # GET/DELETE /api/dataset/:id
│       ├── profile/[id].py      # GET /api/profile/:id
│       ├── job/[id].py          # GET/POST /api/job/:id
│       ├── airtable.py          # Airtable connect/import/writeback
│       └── search/              # Search API routes
│           ├── score.py         # POST (scoring, 900s timeout)
│           ├── feedback.py      # POST
│           ├── chat.py          # POST (questioning)
│           ├── global_rules.py  # GET/POST
│           ├── searches.py      # GET (list)
│           ├── searches/[id].py # GET (detail)
│           └── searches/[id]/   # results, synthesize, rerun, rename
├── enrichment/                  # Core pipeline (unchanged)
└── search/                      # Core search (unchanged)
```

## Key design decisions

- **Chunked enrichment**: Each `/api/enrich` call processes 10 profiles and returns. Frontend loops until done. Progress survives function restarts via Supabase job/profile state.
- **Rewrites**: `vercel.json` maps `/api/*` → `/cloud/api/*` so API files stay organized under `cloud/`.
- **Auth**: HMAC-SHA256 signed session cookies. No external auth deps.
- **Storage**: Service key bypasses RLS; adapter filters by account_id. RLS still protects direct DB access.
- **Scoring**: Runs synchronously (up to 900s). Works for datasets under ~500 profiles.
