# Enrichment Pipeline — Architecture & API Reference

This is the data ingestion layer for People Search. It takes arbitrary CSV/JSON uploads of people, detects the schema, enriches profiles (LinkedIn, web links), and produces compact profile cards that the v2 LLM judge scores.

## How It Works

```
User drops CSV/JSON
    │
    ▼
Schema Detection (enrichment/schema.py)
    │  Auto-classifies columns: identity / links / content / metadata / ignore
    │  Validates column names against sample data (e.g., "LinkedIn" with "Yes/No" → metadata, not URL)
    │  Multiple name fields (First Name + Last Name) get concatenated
    ▼
Cost Estimation (enrichment/costs.py)
    │  Shows: N profiles × $X for LinkedIn enrichment, M email lookups
    │  Cross-dataset dedup: flags profiles already in other datasets
    ▼
Identity Resolution (enrichment/identity.py)
    │  For profiles with name but no LinkedIn URL:
    │  Searches Brave for LinkedIn profiles using ALL available context
    │  (name + org + title + city + country + email domain)
    │  Validates matches against context — stricter when more context available
    ▼
LinkedIn Enrichment (enrichment/enrichers.py)
    │  For profiles with LinkedIn URLs (original or resolved):
    │  Calls EnrichLayer API → full experience, education, headline, about
    ▼
Link Fetching (enrichment/fetchers.py)
    │  GitHub → public API (bio, repos)
    │  Websites → scrape about/bio text
    │  Twitter/X → Brave Search for bio extraction
    │  Google Drive → gws CLI (Docs as text, PDFs with PyPDF2)
    ▼
Profile Card Builder (enrichment/summarizer.py)
    │  Classifies each field: first_person / expert_assessment / linkedin / self_reported
    │  First-person (call notes, transcripts) → kept mostly intact
    │  Self-reported (pitches, essays) → compressed to one-liner
    │  LinkedIn → structured, trimmed to top 5 roles
    │  Output: profile.profile_card (compact text for LLM judge)
    ▼
Saved as Dataset (datasets/<id>.json + datasets/<id>_embeddings.npz)
```

## File Structure

```
enrichment/
├── __init__.py          # Exports: EnrichmentPipeline, SchemaDetector, etc.
├── pipeline.py          # Main orchestrator — wires all steps together
├── schema.py            # Column auto-detection + field type classification
├── models.py            # Profile and Dataset dataclasses
├── identity.py          # Email/name → LinkedIn URL resolution via Brave Search
├── enrichers.py         # LinkedIn enrichment via EnrichLayer API
├── fetchers.py          # GitHub, website, Twitter/X, Google Drive content fetchers
├── summarizer.py        # Profile card builder (compresses verbose fields)
├── dedup.py             # Cross-dataset duplicate detection
├── costs.py             # Cost estimation before enrichment
├── SETUP.md             # API key setup instructions for new users
├── INTAKE_SPEC.md       # Design spec and rationale
├── README.md            # This file
└── test_fixtures/       # Test CSVs for different upload scenarios
    ├── minimal_emails.csv
    ├── crm_export.csv
    ├── conference_attendees.csv
    ├── research_database.csv
    └── messy_google_contacts.csv

upload_web.py              # Flask web app (localhost:5556) — upload UI + dataset management
datasets/                  # Saved datasets (JSON + .npz)
uploads/                   # Temporary uploaded files
```

## Data Model

### Profile (`enrichment/models.py`)

```python
@dataclass
class Profile:
    # Identity
    id: str                    # auto-generated UUID
    name: str                  # full name (First + Last concatenated if separate)
    email: str
    linkedin_url: str          # set by upload OR resolved by identity.py
    organization: str
    title: str
    phone: str

    # Links
    twitter_url: str
    website_url: str
    resume_url: str
    other_links: list[str]

    # Enriched LinkedIn data (from EnrichLayer)
    linkedin_enriched: dict    # {full_name, headline, experience[], education[], context_block}

    # User-uploaded content fields (arbitrary — varies by dataset)
    content_fields: dict[str, str]   # e.g. {"call_notes": "...", "pitch": "..."}

    # Metadata (structured, filterable)
    metadata: dict[str, Any]         # e.g. {"tags": "biosec", "priority": "8"}

    # Fetched link content
    fetched_content: dict[str, str]  # e.g. {"github": "Bio: ...", "website": "..."}

    # Summarized profile card (what the LLM judge reads)
    profile_card: str                # compact text, ~200-500 chars
    field_summaries: dict[str, str]  # cached per-field summaries

    # Pipeline state
    enrichment_status: EnrichmentStatus  # pending / enriched / failed / skipped
```

### Dataset (`enrichment/models.py`)

```python
@dataclass
class Dataset:
    id: str
    name: str
    created_at: str
    source_file: str
    total_rows: int
    profiles: list[Profile]
    field_mappings: list[dict]       # schema detection results
    searchable_fields: list[str]     # which text fields are available
    enrichment_stats: dict           # {resolved, enriched, failed, skipped, ...}
```

## How the v2 Search System Should Use This

### Loading profiles for scoring

```python
from enrichment.pipeline import EnrichmentPipeline

pipeline = EnrichmentPipeline(data_dir="./datasets")

# List available datasets
datasets = pipeline.list_datasets()
# → [{"id": "abc123", "name": "EAG Contacts", "profiles": 105, ...}]

# Load a dataset
ds = pipeline.load("abc123")

# Each profile has a profile_card ready for the LLM judge
for p in ds.profiles:
    print(p.profile_card)  # compact text for scoring
```

### Converting to v2 Profile format

The enrichment `Profile` has more fields than the v2 `Profile` (enrichment status, links, etc.). To convert:

```python
from v2.models import Profile as V2Profile, ProfileIdentity, ProfileField

def to_v2(p):
    """Convert enrichment Profile → v2 Profile for scoring."""
    fields = {}
    for name, text in p.content_fields.items():
        ftype = classify_field_type(name, text)  # from summarizer.py
        fields[name] = ProfileField(value=text, type=ftype)
    if p.linkedin_enriched.get("context_block"):
        fields["linkedin"] = ProfileField(
            value=p.linkedin_enriched["context_block"], type="linkedin"
        )
    for name, text in p.fetched_content.items():
        fields[f"fetched_{name}"] = ProfileField(value=text, type="self_reported")

    return V2Profile(
        id=p.id,
        identity=ProfileIdentity(
            name=p.name, email=p.email, linkedin_url=p.linkedin_url
        ),
        fields=fields,
        raw_text=p.profile_card,  # pre-summarized
    )
```

### What `profile_card` contains

The profile card is what the LLM judge reads. It's built by `summarizer.py`:

- **Header**: `Name | Title at Organization`
- **First-person fields** (call notes, transcripts): kept mostly verbatim (truncated at 600 chars)
- **Expert assessments** (recommendations, evaluations): kept mostly verbatim
- **LinkedIn**: structured summary (headline, top 5 roles)
- **Self-reported text** (pitches, bios, essays): compressed to one line ("Pitch: Building AI-enabled biological design tools.")

Fields are ordered by priority: first-person > expert > linkedin > self-reported.

### Searchable fields per dataset

Each dataset may have different content fields depending on what was uploaded:

```python
ds.searchable_fields
# Dataset A: ["linkedin", "call_notes", "meeting_transcript"]
# Dataset B: ["linkedin", "pitch", "problem", "author_assessment"]
# Dataset C: ["linkedin", "bio", "interview_notes", "key_publications"]
```

The search system should handle dynamic fields — don't hardcode field names.

## Web App API (`upload_web.py`)

Flask app at `localhost:5556`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Upload & management UI |
| `/api/detect-schema` | POST | Upload file, get auto-detected schema |
| `/api/prepare` | POST | Parse file with confirmed mappings, get cost estimate |
| `/api/enrich` | POST | Start enrichment (identity resolution + LinkedIn + links + cards) |
| `/api/embed-only` | POST | Skip enrichment, just build profile cards from existing content |
| `/api/reenrich` | POST | Re-run enrichment on existing dataset (resets all statuses) |
| `/api/job/<id>` | GET | Poll enrichment job progress |
| `/api/job/<id>/cancel` | POST | Cancel running enrichment |
| `/api/datasets` | GET | List all datasets |
| `/api/dataset/<id>` | GET | Get full dataset with all profiles |
| `/api/dataset/<id>` | DELETE | Delete a dataset |

## Environment Variables

```bash
BRAVE_API_KEY          # Required for identity resolution + Twitter bio fetching
ENRICHLAYER_API_KEY    # Required for LinkedIn profile enrichment
ANTHROPIC_API_KEY      # Required for LLM-based summarization (optional)
GWS_CONFIG_DIR         # Google Drive config (default: ~/.config/gws-personal)
```

## Storage

- **`datasets/<id>.json`** — dataset metadata + all profiles as JSON
- **`uploads/`** — temporary uploaded CSV/JSON files

JSON files are sufficient for local use with hundreds to low thousands of profiles. Switch to SQLite when: multi-user, >10K profiles, or concurrent writes needed.
