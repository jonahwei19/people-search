# Enrichment Pipeline — Setup Guide

The enrichment pipeline fetches content from various sources to build rich, searchable profiles. Each fetcher has its own requirements.

## Quick Status Check

```bash
cd projects/candidate-search-tool
python3 -c "from enrichment.fetchers import *; print('Imports OK')"
```

---

## Fetchers

### GitHub (works out of the box)

**No setup needed.** Uses the public GitHub API (unauthenticated).

- Rate limit: 60 requests/hour (unauthenticated)
- For higher limits, set `GITHUB_TOKEN` env var (5,000 req/hr)
- Fetches: bio, company, location, top 5 repos

### Personal Websites (works out of the box)

**No setup needed.** Plain HTTP requests with basic HTML-to-text extraction.

- No auth required
- Extracts readable text from any public webpage
- Filters out nav, footer, cookie banners

### Twitter/X (requires Brave API key)

The X API free tier is unusable (1 request per 15 minutes). We use **Brave Search** instead — search for the X profile and extract the bio from search results. Fast, cheap, and works without X API access.

**Setup:**
```bash
# 1. Get a Brave Search API key at https://brave.com/search/api/
#    (Free tier: 2,000 queries/month. Paid: $0.001/query)

# 2. Set the env var
export BRAVE_API_KEY="your-key-here"

# 3. Test
python3 -c "
from enrichment.fetchers import TwitterFetcher
f = TwitterFetcher()
r = f.fetch('https://x.com/elikirbat')
print(r)
"
```

**What it fetches:** Display name, bio/description from the X profile. Extracted from Brave Search results, not the X API directly.

**Cost:** ~$0.001 per profile (one Brave Search query). For 1,000 profiles: ~$1.

**Alternative (if you have X API access):**
If you have an X API Basic plan ($100/month), you could use the `/2/users/by/username/:username` endpoint directly. This gives more data (follower count, pinned tweet, etc.) but is expensive for what we need. The Brave approach is sufficient for bio extraction.

### Google Drive (requires gws CLI)

Fetches document content from Google Drive links — Google Docs exported as text, PDFs with text extraction, other files best-effort.

**Prerequisites:** The `gws` (Google Workspace CLI) must be installed and authenticated.

**Setup:**

```bash
# 1. Install gws CLI (if not already installed)
#    https://github.com/googleworkspace/cli
#    On macOS:
brew install googleworkspace/tap/gws

# 2. Source gcloud (gws dependency)
source ~/google-cloud-sdk/path.zsh.inc

# 3. Configure gws with a Google account
#    The pipeline defaults to gws-personal config.
#    If already configured (check: ls ~/.config/gws-personal/), skip this.

#    To set up a new config:
mkdir -p ~/.config/gws-personal
#    Copy client_secret.json from your GCP project (OAuth 2.0 credentials)
#    into ~/.config/gws-personal/
#    Then authenticate:
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-personal gws drive about get --params '{"fields": "user"}'
#    This opens a browser for OAuth consent on first run.

# 4. Test
python3 -c "
from enrichment.fetchers import GoogleDriveFetcher
f = GoogleDriveFetcher()
# Test with a public Google Doc
r = f.fetch('https://docs.google.com/document/d/YOUR_DOC_ID/edit')
print(r)
"
```

**What it fetches:**
- **Google Docs** → exported as plain text (full content)
- **PDFs** → downloaded and text-extracted (requires `pip install PyPDF2`)
- **Other files** → downloaded, read as text if possible

**Which Google account to use:**
- The file must be accessible to the authenticated account
- "Anyone with the link" files work with any account
- Private files need the specific account they were shared with

**To use a different Google account:**
```bash
export GWS_CONFIG_DIR=~/.config/gws-ifp  # or gws-bluedot
```

**Available accounts (already configured on this machine):**
| Config dir | Account |
|-----------|---------|
| `~/.config/gws-personal` | weinbaumjonah@gmail.com (default) |
| `~/.config/gws-ifp` | jonah@ifp.org |
| `~/.config/gws-bluedot` | jonah@bluedot.org |

**Cost:** Free (Google Drive API has generous quotas for read operations).

---

## LinkedIn Enrichment (existing)

Uses the EnrichLayer API to pull full LinkedIn profiles (experience, education, about section).

```bash
export ENRICHLAYER_API_KEY="your-key-here"
```

**Cost:** $0.01 per profile.

---

## Email → LinkedIn Resolution (planned)

Uses the [email-to-linkedin](https://github.com/jonahwei19/email-to-linkedin) pipeline to resolve email addresses to LinkedIn profiles.

**Requirements:**
- `BRAVE_API_KEY` — web search
- `SERPER_API_KEY` — Google search (optional but improves accuracy)
- `BRIGHTDATA_API_KEY` — LinkedIn scraping verification (optional)

---

## Environment Variables Summary

```bash
# Required for LinkedIn enrichment
export ENRICHLAYER_API_KEY="..."

# Required for Twitter/X bio fetching
export BRAVE_API_KEY="..."

# Optional: for email→LinkedIn resolution
export SERPER_API_KEY="..."
export BRIGHTDATA_API_KEY="..."

# Optional: override Google Workspace config dir
export GWS_CONFIG_DIR="~/.config/gws-personal"

# Optional: higher GitHub API rate limits
export GITHUB_TOKEN="..."

# Required for LLM-based features (summarization, search scoring)
export ANTHROPIC_API_KEY="..."
```

---

## Troubleshooting

### "gws CLI not found"
```bash
# Check if gws is installed
which gws

# If not, install it
brew install googleworkspace/tap/gws

# Source gcloud (required dependency)
source ~/google-cloud-sdk/path.zsh.inc
```

### "Could not access file" (Google Drive)
- The file may be private. Ask the file owner to set sharing to "Anyone with the link."
- The authenticated Google account may not have access. Try a different account:
  ```bash
  export GWS_CONFIG_DIR=~/.config/gws-ifp
  ```

### "BRAVE_API_KEY not set"
```bash
# Get a key at https://brave.com/search/api/
export BRAVE_API_KEY="your-key"
```

### PDF text extraction fails
```bash
pip install PyPDF2
# or
pip install pdfplumber
```
