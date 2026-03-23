# Enrichment Intake — Design Spec

## The Problem

Someone has a database of people. Could be 50 contacts from a conference, 500 CRM entries, 10,000 newsletter subscribers. They want to upload it and search over it. Their data comes in wildly different shapes — different column names, different levels of richness, different things they know about each person.

The intake system needs to:
1. Accept whatever they have
2. Figure out what's there
3. Make it searchable immediately (no API calls needed)
4. Enrich what's missing in the background (LinkedIn profiles, identity resolution)
5. Not be a hassle

---

## The Core Tension

The search system works by embedding text fields and doing cosine similarity. It needs structured data: here are the text fields for this person, here are their weights. But users' data isn't structured that way — it's whatever columns their CRM or spreadsheet happens to have.

The enrichment layer's job is to bridge this gap: take messy, inconsistent input and produce clean, searchable profiles. The hard part isn't any individual step — it's handling the combinatorial variety of what people might upload.

---

## What We Need From Each Person

To make someone searchable, we need at minimum:
- **An identity** — who is this person? (name, email, LinkedIn URL — any combination)
- **Something to search over** — text that describes who they are and what they do

The ideal profile has:
- Full LinkedIn profile (experience, education, summary) — the universal background signal
- First-person data (call notes, interview transcripts, recommendations) — the high-value proprietary signal
- Self-reported data (bios, pitches, application essays) — useful but take with a grain of salt

---

## Information Priority

Not all text fields are created equal. The search system uses per-field weights, and the defaults should reflect information value:

| Data Type | Default Weight | Why |
|-----------|---------------|-----|
| **First-person observations** (call notes, interview transcripts, meeting notes) | HIGH (0.35) | This is what you can't get anywhere else. "She wants to leave Anthropic to work on bio screening" is uniquely valuable. |
| **Expert assessments** (recommendations, evaluations, author assessments) | HIGH (0.25) | Trusted third-party signal. "Deep technical knowledge but less policy aware." |
| **LinkedIn profile** (enriched) | MEDIUM (0.25) | Universal background. Good for "has DARPA experience" queries. Everyone has it, so it doesn't differentiate as much. |
| **Self-reported text** (bios, pitches, about sections) | LOW (0.10) | Often aspirational. "Passionate about solving biosecurity" doesn't mean they can. |
| **Short metadata** (tags, categories) | FILTER only | Not embedded — used for structured filtering. |

**But these defaults are wrong for some queries.** "Find someone with 10 years in arms control" → LinkedIn dominates. "Find someone who expressed interest in switching careers" → call notes dominate. The query planner should adjust weights dynamically, and the user should be able to override via sliders.

The system should classify each content field into one of these tiers and set defaults accordingly. The classification heuristic:
- Field name contains "notes", "transcript", "call", "interview", "meeting" → first-person
- Field name contains "assessment", "evaluation", "recommendation", "review" → expert
- Field name contains "linkedin", "experience", "education" → enriched profile
- Field name contains "bio", "pitch", "about", "description", "summary" → self-reported
- Can't tell → medium weight (0.20)

---

## Intake Flow

### Phase 1: Instant (no API calls, seconds)

**Step 1: Upload**
User drops a CSV or JSON. System reads it.

**Step 2: Schema detection**
Auto-classify each column:
- **Identity fields** (green): name, email, LinkedIn URL, organization, title, phone
- **Content fields** (blue): any text column with substantial content → will be embedded for search
- **Metadata fields** (gray): tags, categories, dates, scores → used for filtering
- **Ignored fields** (collapsed): IDs, timestamps, internal system fields

Show a clean summary, not a 40-row table. Group by type. Example:

```
Identity:  Name (✓ found in 398/400 rows), Email (✓ 400/400), LinkedIn URL (✓ 342/400)
Searchable: Internal Notes, Meeting Transcript, Bio
Filters:   Tags, Deal Stage, Priority Score
Skipping:  record_id, created_at
```

User can click any field to change its classification. But the goal is that most users just scan and hit "Confirm."

**Step 3: Immediate embedding**
Embed whatever content fields we have RIGHT NOW. If the upload has call notes and bios, those are searchable in seconds. No need to wait for LinkedIn enrichment.

After Phase 1: profiles are searchable using whatever data the user uploaded. If they uploaded rich data (notes, transcripts), search works well immediately.

### Phase 2: Background enrichment (API calls, costs money)

**Step 4: Cost estimate**
Before any API calls, show what enrichment will cost:

```
Ready to enrich:
  342 profiles have LinkedIn URLs → enrich via EnrichLayer ($3.42)
   58 profiles have email only    → find LinkedIn via search ($0.29-$3.65)
    0 profiles have name only     → identity resolution not available

Total: $3.71 – $7.07 (depending on email→LinkedIn hit rate)
Estimated time: ~15 min (LinkedIn) + ~1 hr (email resolution)
```

User approves or skips enrichment. If they skip, search still works with what they uploaded.

**Step 5: Run enrichment**
- LinkedIn enrichment runs for profiles with URLs (EnrichLayer, $0.01/profile)
- Email → LinkedIn resolution runs for email-only profiles (using the email-to-linkedin pipeline)
- Progress bar with live updates
- Incremental saves (never lose progress)
- After each batch, re-embed the enriched profiles so search improves in real-time

After Phase 2: profiles have full LinkedIn data added. Search quality improves because there's more text to match against.

---

## Schema Detection: What Works and What Doesn't

### Current auto-detection results on test fixtures

I ran 5 test CSVs through the schema detector. Results:

**conference_attendees.csv** — PROBLEM
```
  "Attendee" → metadata  ← WRONG: should be identity_name
```
The detector doesn't recognize "Attendee" as a name column. It's not in the pattern list. This is the kind of creative column name that requires either:
- A bigger pattern list (brittle, always incomplete)
- LLM-based classification (accurate but slow/costly)
- Good enough defaults that the user catches it

**messy_google_contacts.csv** — PROBLEMS
```
  "Given Name"              → metadata  ← should be used for name (or at least flagged)
  "Organization 1 - Title"  → metadata  ← should be identity_title
  "E-mail 2 - Value"        → metadata  ← should be treated as a secondary email
```
Google Contacts uses verbose column names with numbers and dashes. The detector's regex doesn't handle "Organization 1 - Title" because of the "1 - " prefix.

**crm_export.csv** — CLEAN
All columns correctly classified. This is the happy path: standard CRM naming conventions.

**research_database.csv** — BORDERLINE
```
  "research_area" → metadata  ← arguable: it's short text, but valuable for search
```
"AI governance and biosecurity ethics" is 37 chars. The detector classified it as metadata because avg length < 30 (based on 3 samples). A user might want this as a content field. Either way, easy to fix in the UI.

**minimal_emails.csv** — CLEAN
Simple case, nothing to get wrong.

### Improvement options

**Option A: Bigger pattern list.** Add "attendee", "participant", "contact", "person", "applicant", "candidate", "respondent" etc. to the name patterns. Add "given.?name", "first", "last" as name sub-patterns. This catches 80% more cases but is always incomplete.

**Option B: LLM classification.** Send the column names + 3 sample values to Claude and ask "classify each column." This is more accurate but adds ~$0.01 and 2 seconds per upload. Worth it for a product; overkill for a prototype.

**Option C: Data-driven heuristics.** If a column's values look like person names (capitalized words, 2-3 words, no numbers), classify as name even if the column header is unusual. This catches "Attendee" because "Dr. Fumiko Tanaka" looks like a name.

**Recommendation: A + C for now, B later.** Expand the pattern list and add a name-detection heuristic on sample values. Reserve LLM classification for when we need high accuracy on weird schemas.

---

## The Email-to-LinkedIn Pipeline

The existing [email-to-linkedin](https://github.com/jonahwei19/email-to-linkedin) repo solves identity resolution: given an email address, find the person's LinkedIn profile.

**When to use it:**
- User uploads profiles with email but no LinkedIn URL
- This happens with: Google Contacts exports, CRM exports where LinkedIn wasn't captured, newsletter subscriber lists

**How it works:**
1. Searches the web for the exact email (ground truth)
2. Parses the person's name from the email + search results
3. Runs LinkedIn-specific searches
4. Scores candidate profiles (name match, company match, email evidence)
5. Optionally verifies via Bright Data scraping

**Cost per lookup:** ~$0.007-0.063 depending on which APIs are used
- Brave Search: ~$0.001/query × 5 queries = $0.005
- Serper: ~$0.002/query × 5 queries = $0.01
- Bright Data (optional): ~$0.50-1.00/profile
- ContactOut (optional): ~$5/lookup

**For cost estimation:** use $0.005/lookup as baseline (Brave only), $0.015/lookup for Brave+Serper, up to $0.06 with Bright Data verification.

**Integration:** After the email-to-linkedin pipeline finds a LinkedIn URL, feed that URL into EnrichLayer for full profile enrichment. Two-step: resolve → enrich.

**Accuracy expectations:**
- Corporate email + full name: ~85% correct match
- Personal email (gmail, etc.): ~50-60% — much harder, often not worth the cost
- No parseable name from email: skip entirely

**UX implication:** When showing the cost estimate, distinguish between:
- "58 profiles have corporate emails — we can likely find their LinkedIn (~85% success, $0.87)"
- "12 profiles have personal emails — lower success rate (~50%, $0.18)"
- User can choose to skip personal emails to save money and avoid false matches.

---

## How Uploaded Data Feeds Into Search

The current search system hardcodes 5 fields. With the enrichment layer, this needs to become dynamic. The changes:

### Current (hardcoded)
```python
FIELDS = ["pitch", "problem", "solution", "linkedin", "author_assessment"]
DEFAULT_WEIGHTS = {"pitch": 0.05, "problem": 0.05, "solution": 0.02, "linkedin": 0.80, "author": 0.08}
```

### Needed (dynamic)
```python
# Each dataset declares its fields
dataset.searchable_fields = ["linkedin", "internal_notes", "meeting_transcript", "bio"]

# Default weights computed from field type classification
DEFAULT_WEIGHTS = compute_default_weights(dataset.searchable_fields)
# → {"linkedin": 0.25, "internal_notes": 0.35, "meeting_transcript": 0.35, "bio": 0.10}
```

### What this means for search_web.py

1. **Load multiple datasets.** Instead of one `search_data.json`, load from `datasets/` directory. Each dataset has its own profiles and embeddings.

2. **Dynamic field list.** The UI weight sliders, the embedding lookup, and the planner all need to work with whatever fields exist in the loaded dataset(s).

3. **Cross-dataset search.** If two datasets are loaded (one with [notes, linkedin] and another with [pitch, linkedin, assessment]), search should work across both. The common field (linkedin) matches in both; dataset-specific fields only match within their dataset. Missing fields get a zero score contribution, not a crash.

4. **Per-field weight computation.** When a new dataset is loaded, compute default weights based on field type classification. The planner and the weight sliders use these defaults.

---

## Test Cases

### Test Case 1: Minimal emails (`minimal_emails.csv`)
- 5 people, name + email only
- **Immediate searchability:** zero — nothing to embed
- **Enrichment needed:** email → LinkedIn for all 5
- **Expected UX:** "These profiles have no searchable content yet. We can enrich them via LinkedIn ($0.05 for LinkedIn enrichment after email resolution). Without enrichment, there's nothing to search."
- **Edge case:** If user doesn't want to pay for enrichment, the upload is basically useless for search. Should we say so clearly?

### Test Case 2: Rich CRM export (`crm_export.csv`)
- 5 people, most have name + email + LinkedIn + notes + transcripts
- **Immediate searchability:** HIGH — notes and transcripts are embedded right away
- **Enrichment:** 4 LinkedIn URLs to enrich ($0.04), 1 email-only lookup ($0.005-0.06)
- **Expected UX:** "2 searchable fields detected: Internal Notes, Meeting Transcript. 4 LinkedIn profiles will be enriched. Profiles are searchable now — enrichment will add LinkedIn background data."
- **Key test:** Search for "someone interested in AI governance" should find Sarah Martinez and Wei Zhang from their call notes alone, BEFORE LinkedIn enrichment runs.

### Test Case 3: Conference attendees (`conference_attendees.csv`)
- 5 people, name + org + title + bio. No email, no LinkedIn.
- **Immediate searchability:** bios are embedded right away
- **Enrichment:** none possible without email or LinkedIn URL
- **Schema problem:** "Attendee" column not recognized as name
- **Expected UX:** Schema review should make it easy to fix "Attendee" → Name. After that, bios are searchable. System should note: "No email or LinkedIn found — enrichment not available. To enrich these profiles later, add their emails or LinkedIn URLs."

### Test Case 4: Research database (`research_database.csv`)
- 4 people, all have name + LinkedIn URL + rich notes + publications
- **Immediate searchability:** HIGH — interview notes and publications embedded
- **Enrichment:** 4 LinkedIn URLs to enrich ($0.04)
- **Key test:** The interview notes are the highest-value content. "Find someone thinking about leaving their job" should find Wei Zhang (from CRM test) via transcript, or Mark Thompson from interview notes about being "primarily motivated by research."
- **Weight test:** With default weights, interview_notes should dominate. A query about career backgrounds should shift weight toward LinkedIn.

### Test Case 5: Messy Google Contacts (`messy_google_contacts.csv`)
- 5 rows but one is an unsubscribe entry, one is just "Bob" with no info
- **Schema problems:** Given Name/Family Name not recognized, "Organization 1 - Title" missed
- **Garbage rows:** Row 2 (newsletter@substack.com) and Row 4 (just "Bob") should ideally be flagged or filtered
- **Expected UX:** After schema review, 3 of 5 rows are meaningful. Notes are short but still searchable.
- **Edge case:** How do we handle empty/junk rows? Filter on upload? Show a count of "3 profiles with searchable content, 2 skipped (no identity or content)"?

### Test Case 6: The actual TLS data (existing `search_data.json`)
- 399 people with pitch, problem, solution, LinkedIn, author_assessment, category
- This is the system's current data — the new enrichment layer should be able to ingest this format too
- **Key test:** If someone re-uploads the TLS data through the new intake system, does it produce equivalent results to the current hardcoded pipeline?

---

## Open Design Questions

### Q1: What happens with multiple datasets?
If I upload my CRM contacts AND a conference attendee list, are these:
- **(a) Merged** — all profiles go into one searchable pool, deduplicated by name/email
- **(b) Separate** — each dataset is a distinct searchable collection, user picks which to search
- **(c) Both** — datasets exist separately but can be searched together or independently

Recommendation: **(c)** — upload creates a dataset; search defaults to "all datasets" but can be filtered. Dedup happens at display time (highlight "this person appears in 2 datasets") not at storage time.

### Q2: How much should we try to clean the data vs. show the user?
Messy rows (empty names, junk emails, duplicates) — do we:
- Auto-filter and show a summary ("removed 12 rows with no usable data")?
- Show everything and let the user decide?
- Auto-filter but provide an "excluded rows" view?

Recommendation: Auto-filter with summary. "400 rows uploaded. 387 profiles created. 13 rows skipped (8 no name, 3 duplicate emails, 2 no content)."

### Q3: Should the upload interface be part of search_web.py or separate?
Currently the upload UI is a separate Flask app (upload_web.py on port 5556). It could also be a tab within the main search app.

Recommendation: Separate for now. They're different workflows — upload/manage is an admin task, search is the daily-use tool. Merge later when the product matures.

### Q4: Re-embedding after enrichment
When LinkedIn enrichment completes for a profile, we need to re-embed the LinkedIn text and add it to the embeddings. This means:
- Embeddings are not static — they grow as enrichment runs
- The search system needs to hot-reload embeddings
- If a profile had notes-only embeddings and then gets LinkedIn, it now has 2 fields instead of 1

This is straightforward but needs to be designed carefully to avoid data corruption (partial writes, race conditions between search and enrichment).

### Q5: Identity dedup across datasets
Sarah Martinez appears in both the CRM export and the Google Contacts export. How do we detect this?
- Same email → definite match
- Same name + same org → likely match
- Same LinkedIn URL → definite match

When detected: don't delete either. Show "2 profiles for Sarah Martinez" with the ability to view data from both sources. The search system treats her as one person with fields from both datasets.

---

## Implementation Priority

1. **Fix schema detection** — add more name patterns, add data-driven heuristics (Option A+C from above)
2. **Integrate email-to-linkedin pipeline** — import and adapt for use as an enrichment step
3. **Dynamic field support in search** — modify search_web.py to handle arbitrary fields
4. **Build the upload UI** — the upload_web.py prototype needs the grouped summary view (not a raw table)
5. **Cross-dataset search** — support loading/searching multiple datasets
