# People Search System — Spec

## Part A: What We're Building

### The Product

An app. A team downloads it (or accesses it hosted). They upload a database of people — could be a CSV of names and emails, LinkedIn URLs, whatever they have. The app enriches those people automatically (finds their LinkedIn, pulls their experience, builds a full profile). Everything is stored locally, auditable, private.

The team then defines searches. Not one-off queries — defined, named searches like "Voyage candidates" or "Biosecurity advisors" or "People we should recruit." These searches persist. The app returns the top 10-100 people from the database who match.

The team gives feedback on results: this person is great (and here's why), this person is wrong (and here's why). That feedback teaches the system what the team means by their search terms. Next time they upload a new database — a new batch of applicants, a partner's contact list, a conference attendee list — those same defined searches work better automatically, because the system already knows what "Voyage candidate" means from the previous round.

### Two Problems

**Problem 1: Enrichment.** Given sparse inputs (name + email, or name + LinkedIn URL), produce a rich searchable profile.
- Identity resolution: given "Jane Smith" and an email, find the right Jane Smith on LinkedIn
- LinkedIn enrichment: pull full experience, education, about section
- Structured summary: produce the fields that matter for search
- Incremental: works on new uploads without re-processing existing data

**Problem 2: Search that learns.** Given enriched profiles, run defined searches that get smarter over time.
- A search is a persistent, named thing — not a one-off query box
- Feedback on results calibrates what the search means across the whole database
- When certain words get used across searches ("exceptional entrepreneur," "senior," "fluff"), the system learns what those words mean to this team
- When external users run searches, the system bridges their vocabulary to the team's calibrated understanding through questioning
- When a new database is uploaded, existing searches immediately return useful results because the calibration carries over

### How It Gets Used

1. **Internal:** A team uploads 400 applicants. Defines a search "Voyage candidates." Rates results. Next cohort of 300 comes in — upload, run same search, top 50 are ready.
2. **Shareable:** Send the app to a partner org. They upload their own contact list. The app has the team's calibrated understanding baked in. Partner searches "who in our network could advise on biosecurity?" and gets results ranked by the team's standards, not generic LinkedIn matching.
3. **Cross-search calibration:** The word "exceptional" means the same thing across all searches because it was calibrated through feedback on multiple searches. A new search "exceptional climate policy operators" benefits from feedback given on "exceptional biosecurity entrepreneurs" because "exceptional" has been defined.

### Current Test Case

The first deployment is for Jonah Weinbaum's team reviewing TLS (The Launch Sequence) applicants — ~400 people who applied to a program funding AI resilience projects. Each applicant has a pitch, problem statement, solution, LinkedIn profile, and an AI-generated author assessment. The search tool is being built generically but tested against this dataset.

---

## Part B: What's Implemented (as of 2026-03-22)

### Data Pipeline (Problem 1 — partially solved, TLS-specific)
- `fetch_applicants.py` — pulls applicants from Airtable (TLS-specific, needs generalization)
- `enrich_linkedin.py` — enriches via EnrichLayer API. 267/399 profiles have full LinkedIn data.
- `dedup.py` — merges duplicate submissions. 466 → 399 unique people.
- `search_data.json` — 399 people with: pitch, problem, solution, LinkedIn text, author assessment, category, decision, linkedin_url (TLS schema — generic version needs configurable fields)
- `search_embeddings.npz` — pre-computed embeddings across 5 text fields

### Search (Problem 2 — partially built, feedback loop broken)
- `search_web.py` — Flask app at localhost:5555
  - Multi-field embedding search (cosine similarity, numpy, ~2s startup)
  - Progressive questioning (Claude Sonnet asks 1-2 clarifying questions, dynamic per query)
  - "Find similar" button (uses person's embeddings as query)
  - Ratings (★, ✓, ✗, ✗✗) with text reasons per result
  - Session state: negative ratings hide results, positive boost scores
  - Auto-learn: Sonnet evaluates text reasons and may generate new priors
  - Query augmentation: priors appended to query text before embedding
  - LinkedIn links on results
- `questioning.py` — LLM intent planner (requires ANTHROPIC_API_KEY)
- `scoring.py` — post-retrieval score adjustments (mostly broken, see below)
- `extract_features.py` — keyword-based feature extraction (mostly wrong, see below)
- `priors.jsonl` — 7 seed priors
- `eval.py` + `eval_ui.py` — eval harness with 9 hand-ranked queries

### What's on GitHub
- Nothing pushed yet for the search tool. Needs a new repo.

### Known Broken Things
1. **Feature extraction is garbage.** Keyword counting produces wrong results — 299/398 flagged as government experience, 260/398 as executive-level seniority.
2. **Scorer does almost nothing.** Applies tiny additive adjustments (+0.04) based on the broken features. Net-negative on eval (-0.015 NDCG). Three hardcoded if-statements.
3. **Feedback doesn't actually change search.** Ratings and reasons go into `feedback_log.jsonl`. Auto-learn sometimes adds a prior to `priors.jsonl`. Priors get appended to query text. But: (a) priors are appended to ALL queries indiscriminately, (b) many judgments ("this is fluff") aren't semantic directions, (c) the embedding model can't evaluate gaps between claims and credentials.
4. **Superlinked abandoned at runtime.** 10+ minute startup embedding 466 records. Switched to pre-computed numpy. Superlinked is installed but unused.
5. **No multi-user support.** Single-user local prototype only.
6. **Not deployed anywhere.** Runs on Jonah's laptop.

---

## Part C: Open Architecture Questions

These are the real problems we haven't solved:

### C1: How should feedback change the database's understanding of profiles?

This is the central unsolved problem. The question is NOT "how should feedback change this query's results" — it's "how should feedback change how the system sees every profile."

**What we tried and why it failed:**
- **Post-retrieval score adjustments (scorer):** Adds tiny numbers to cosine similarity scores based on keyword-matched features. Features are wrong (keyword counting). The adjustments are too small to matter. The scorer has 3 hardcoded if-statements. This is not learning.
- **Query augmentation (append priors to query text):** Appends text like "prefer practitioners" to every query before embedding. Works for semantic preferences. Fails for reasoning-based judgments ("this is fluff" is not a direction in embedding space). Also applies indiscriminately to all queries, even when irrelevant.

**The core tension:** Some feedback is semantic ("prefer government experience" = a direction in embedding space). Some requires reasoning ("this person's pitch doesn't match their background" = evaluating a gap between two things). Embeddings handle the first. They can't do the second.

**Most promising approaches not yet tried:**

1. **Per-profile LLM assessment (calibration layer).** Have Claude read each profile once and write a calibration summary: "This person is a credible biosecurity operator with DARPA experience. Strength: has shipped real programs. Weakness: pitch scope may be too narrow." Embed these summaries as a 6th field. When the team gives feedback like "this is the kind of person we want" or "this is fluff," update the calibration summary for that profile and re-embed. The embedding space itself now reflects institutional judgment, not just raw profile text. Cost: ~$4 one-time, ~$0.01 per update.

2. **LLM re-ranking with institutional context.** Take top 30 from embedding search, have Claude read the profiles alongside accumulated institutional knowledge and re-rank. This handles reasoning-based judgments ("pitch doesn't match background") that embeddings can't. Cost: ~$0.05/search. Downside: doesn't change the database, only the output of each search.

3. **Exemplar-based calibration.** Users mark 5-10 "gold standard" profiles and 5-10 "anti-exemplars." The system computes what separates them in embedding space and uses that direction to boost/penalize all profiles. This is lightweight fine-tuning without actually fine-tuning — you're learning a linear classifier on top of frozen embeddings.

These are not mutually exclusive. Option 1 (calibration summaries) changes how profiles are represented. Option 2 (LLM re-ranking) handles per-query reasoning. Option 3 (exemplars) learns the team's preference direction directly.

### C2: Profile Archetypes — How to Encode "What Good Looks Like"

The system needs to know what an "exceptional entrepreneur" looks like for this team without being told on every search. This is a calibration problem, not a search problem.

**Possible approaches:**

- **Exemplar profiles:** "An exceptional entrepreneur looks like Jeff Alstott's profile. NOT like [fluff person's] profile." Store exemplar IDs. Compute the centroid of exemplar embeddings. Use distance-from-centroid as a ranking signal. Simple, interpretable, easy to update — just add/remove exemplars.

- **Calibration summaries (from C1 option 1):** Claude writes a judgment of each profile. Accumulated feedback adjusts these judgments. The judgment text gets embedded. Searches match against both raw profile and calibrated judgment.

- **Vocabulary glossary:** Maintain a lookup: "exceptional entrepreneur" → "someone who has built and shipped products or programs, has 5+ years of operating experience, background matches what they're pitching." The planner expands terms using this glossary before searching. Simple but brittle — every new term needs manual definition.

### C3: Persistent Searches vs. Ephemeral Queries

Current model: type query → get results → give feedback → feedback dies.

Needed model: saved searches that accumulate understanding. A search for "biosecurity advisory candidates" is a living thing you return to. Each visit, results are better because:
- Previous feedback (ratings + reasons) is remembered for this search
- Feedback from this search may have updated profile calibrations affecting other searches too
- New people added to the database since last visit appear in results

This is closer to a "view" or "smart folder" than a search box. Implementation options:
- Store search sessions persistently (not just in-memory) with their accumulated ratings
- Allow naming/saving searches
- Show "new since last visit" indicators

### C4: Multi-user / Shareable

Current: Flask app on localhost. No auth, no multi-tenancy, no data upload.

Needed:
- **Upload interface:** CSV with name/email/LinkedIn → enrichment pipeline → embeddings
- **Shared understanding layer:** Institutional knowledge (calibration summaries, exemplars, priors, glossary) ships with the tool. External users search against the team's calibrated database, not raw embeddings.
- **Vocabulary bridging:** The questioning layer for external users translates their queries into the team's vocabulary. "Find me someone good at AI policy" → system asks clarifying questions that map onto the team's archetype space.
- **Privacy:** Uploaded data stays with the uploader. Shared understanding is the calibration layer, not the data.
- **Deployment:** AWS EC2 available ($24K credits). Or static export for simple sharing.

---

## Part D: Delegation Plan

Three workstreams that can run in parallel. Each agent gets this spec file plus the relevant section below.

### Agent 1: Enrichment Pipeline

**Goal:** Build the "upload a CSV, get back enriched profiles" pipeline. This is the front door of the product — everything else depends on having rich data.

**Input:** CSV with any combination of: name, email, LinkedIn URL, organization, title. Messy real-world data.

**Output:** For each person, a structured profile with: full name, LinkedIn URL, headline, current role, experience history (titles, companies, years), education, about section, location. Stored locally as JSON. Deduped.

**What to build:**
1. CSV parser that handles multiple input formats (name+email, name+LinkedIn, name+org+title)
2. Identity resolution: given a name (+ optional email/org), find the right LinkedIn profile. May need web search, LinkedIn search API, or enrichment APIs.
3. LinkedIn enrichment: given a LinkedIn URL, pull the full profile. We've used EnrichLayer ($0.01/profile, got 267/399). Evaluate alternatives.
4. Structured output: normalize the enriched data into a consistent schema
5. Embedding generation: run the structured profiles through a sentence transformer, store as .npz
6. Incremental updates: when a new CSV is uploaded, only process people not already in the database

**Existing code to start from:** `enrich_linkedin.py`, `fetch_applicants.py`, `dedup.py` in this repo. These work but are TLS-specific — need to be generalized.

**Constraints:** Must work locally (no cloud dependency for data storage). Enrichment APIs are OK (data goes out for lookup, comes back, stored locally).

### Agent 2: Search Calibration

**Goal:** Solve the core search quality problem — how does feedback on search results change the system's understanding of profiles so that future searches (including on new databases) are better?

**The problem in detail:** We have embedding search working (cosine similarity across 5 text fields). It returns plausible results but doesn't reflect institutional knowledge. When the team says "this person is great because they've shipped real programs" or "this person is fluff — impressive pitch but no substance behind it," that feedback needs to change how ALL profiles are evaluated, not just score adjustments on this one search.

**The hard part:** Some feedback is semantic ("prefer government experience" = a direction embeddings can represent). Some feedback requires reasoning ("this person's pitch doesn't match their background" = a gap between two text fields that embeddings can't evaluate). The system needs to handle both.

**Approaches to evaluate:**
1. **Per-profile calibration summaries.** Have an LLM read each profile and write a calibrated assessment (e.g., "credible biosecurity operator" or "student pitching outside expertise"). Embed these as a searchable field. Feedback updates the assessments. Cost: ~$4 for 400 profiles, ~$0.01/update.
2. **Exemplar-based learning.** User marks "gold" and "anti-gold" profiles. System computes what separates them in embedding space and uses that direction for ranking.
3. **LLM re-ranking.** Take top 30 from embeddings, have an LLM re-rank using accumulated institutional knowledge. Handles reasoning. Cost: ~$0.05/search.
4. **Cross-encoder with instructions.** Voyage rerank-2.5 or Contextual AI. ~$0.008/search for 400 profiles. Handles some reasoning via natural language instructions.

**Existing code to start from:** `search_web.py` (working search UI), `scoring.py` (broken scorer), `priors.jsonl` (7 seed priors), `eval.py` (eval harness with 9 ranked queries). The eval harness works — use it to measure whether an approach actually improves results.

**What to deliver:** A working prototype that demonstrates measurable improvement on the eval set (baseline P@5=0.51, NDCG@10=0.66). Show which approach works, what it costs, and how feedback flows from user action → stored learning → changed results.

### Agent 3: Defined Searches & Cross-Search Learning

**Goal:** Design and build the "defined search" abstraction — a named, persistent search that accumulates feedback and gets better over time. And figure out how learning transfers across searches.

**What a defined search is:**
- A named search like "Voyage candidates" with a natural language description
- Accumulated feedback: which profiles were marked good/bad and why
- Possibly: exemplar profiles, calibration priors specific to this search
- When run against any database, returns ranked results reflecting all accumulated feedback

**The cross-search problem:** The word "exceptional" should mean the same thing whether you're searching for "exceptional biosecurity entrepreneurs" or "exceptional climate policy operators." Feedback given on one search should improve others when the same concepts appear.

**What to figure out:**
- Data model for a defined search (what gets persisted?)
- How feedback on search A improves search B (shared vocabulary calibration)
- How to handle new databases (upload new CSV → run existing defined searches → results reflect prior learning)
- How questioning works for external users who don't share the team's vocabulary

**Existing context:** The `questioning.py` file has an LLM-based intent planner that asks clarifying questions. `priors.jsonl` has some global priors. Neither handles the "defined search" concept.

### Requires Jonah
- Mark 5-10 exemplar profiles and 5-10 anti-exemplars (for Agent 2)
- Define 3-5 named searches with descriptions (for Agent 3)
- Complete more eval queries (9/20 done, need ~15+ for meaningful measurement)
- Decide deployment target

---

## File Inventory

```
candidate-search-tool/
├── search_web.py           # Flask app with all features (localhost:5555)
├── questioning.py          # LLM intent planner
├── scoring.py              # Post-retrieval scorer (mostly broken)
├── extract_features.py     # Keyword feature extraction (mostly wrong)
├── eval.py                 # Eval harness
├── eval_ui.py              # Eval ranking UI (localhost:5556)
├── dedup.py                # Duplicate profile merger
├── search.py               # CLI search (original)
├── fetch_applicants.py     # Airtable data puller (TLS-specific, needs generalization)
├── enrich_linkedin.py      # EnrichLayer bulk enrichment
│
├── search_data.json        # 399 merged applicants (TLS test data)
├── search_embeddings.npz   # Pre-computed embeddings (5 fields × 399)
├── profile_features.json   # Derived features (known inaccurate)
├── priors.jsonl            # 7+ global priors (TLS-specific seeds)
├── feedback_log.jsonl      # User feedback events
├── eval_draft.json         # 20 eval queries with results (TLS-specific)
├── eval_set.jsonl          # 9 hand-ranked queries (TLS-specific)
│
├── archive/search_v1/      # Archived earlier versions
├── context for the search project.md
├── what_is_built.md        # Implementation status (outdated, see this file)
└── search_system_spec.md   # THIS FILE
```
