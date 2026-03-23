# TLS Applicant Search — What's Actually Built (as of 2026-03-14)

## Status: Phases 0-2 built, Phase 3 partially built, Phase 4 NOT built

---

## What works

### Data pipeline
- `fetch_applicants.py` — pulls 463 applicants from Airtable
- `enrich_linkedin.py` — enriches LinkedIn profiles via EnrichLayer API (267 with experience data)
- `search_data.json` — 466 applicants with pitch, problem, solution, LinkedIn profile, author assessment, category, decision, recommendations, linkedin_url
- `search_embeddings.npz` — pre-computed embeddings for all 466 applicants across 5 text fields using `sentence-transformers/all-mpnet-base-v2`

### Search engine (`search_web.py` at localhost:5555)
- **Multi-field embedding search**: 5 text fields (pitch, problem, solution, linkedin, author_assessment) embedded separately, combined with adjustable weights at query time via cosine similarity
- **Progressive questioning**: LLM (Claude Sonnet) asks 0-2 clarifying questions before search, starting with purpose. Produces structured intent with adjusted weights and annotations. Falls back to direct search if no API key.
- **Deterministic prior scoring** (`scoring.py`): Applies score adjustments from `profile_features.json` and `priors.jsonl` after retrieval. Additive mode. Shows prior adjustment pills on each result.
- **"Find similar"**: Button on each result card. Uses the person's pre-computed embeddings across all fields as the query vector. Finds people with similar profiles.
- **LinkedIn links**: Clickable links to LinkedIn profiles on each result
- **Score breakdowns**: Expandable detail showing semantic score, prior adjustments, and final score
- **Weight sliders**: Behind "Advanced" toggle. Planner overrides them when questioning is active.

### Feature extraction (`extract_features.py` → `profile_features.json`)
- 398 applicants with derived features:
  - `seniority_score` (0-1, based on titles and years)
  - `practitioner_score` (0-1, practitioner vs academic ratio)
  - `has_government_experience`, `has_think_tank`, `has_ea_safety`, `has_startup_experience`, `has_shipped_products`
  - `org_tags`, `sector_tags`
- Known issues: government experience overcounts (299/398), seniority skews high (260/398 in 0.8-1.0 bucket due to "director" keyword in author assessments catching too broadly)

### Global priors (`priors.jsonl`)
- 7 seed priors (weight-level and entity-level)
- Weight priors: fed to planner as context for weight selection
- Entity priors: applied by scorer via `score_modifier` field
- API endpoints: `GET /api/priors` (list), `POST /api/priors` (add)
- Scorer can reload priors from disk via `SCORER.reload()`

### Eval harness (`eval.py` + `eval_ui.py`)
- `eval_ui.py` at localhost:5556: web UI for hand-ranking results. Top 5 / Relevant / Nope buttons with per-result "why?" text boxes. 20 queries, 9 ranked so far.
- `eval.py`: computes P@5, P@10, Recall@10, MRR, NDCG@10, nope_in_top_10. Supports `--compare` to diff baseline vs priors.
- Current baseline: P@5=0.51, MRR=0.67, NDCG@10=0.66
- Priors are currently net-negative (-0.015 NDCG), with a regression on "AI governance" query

### Intent planner (`questioning.py`)
- Uses Claude Sonnet via Anthropic API
- Asks purpose first, then optional disambiguation
- Produces structured intent: expanded query, weights, annotations
- Reads `priors.jsonl` to inform weight selection
- Session management for multi-turn Q&A
- Falls back gracefully if API key not set

---

## What is NOT built (from the spec)

### Phase 4: Session feedback — NOT BUILT
The spec calls for:
- Thumbs up / thumbs down buttons on each result
- Session state tracking liked/disliked applicants
- Score adjustments in subsequent searches based on feedback within a session
- "More like this" combined with session state (currently "find similar" exists but doesn't track session)
- Feedback events logged for later analysis
- Review loop: aggregate feedback → manually promote to global priors

**None of this exists.** There are no feedback buttons in the UI. There is no session state. The eval rankings and reasons are stored but not read by the search system.

### Duplicate/merged profiles — NOT BUILT
Some applicants submitted multiple pitches and appear multiple times. There is no deduplication or profile merging.

### Eval feedback loop — NOT CONNECTED
The per-result reasons from the eval UI ("why?") are stored in `eval_set.jsonl` but nothing reads them. They could be translated into prior rules but this hasn't been done.

### Cross-encoder reranking — NOT BUILT (by design, Phase 5)
Per spec, this is only added after eval shows deterministic scoring is insufficient.

### NumberSpaces in Superlinked — NOT USED
The spec proposed adding `NumberSpace` for seniority_score, practitioner_score, years_experience as searchable dimensions. The current implementation bypasses Superlinked for search (uses raw numpy for instant startup) and applies these as post-retrieval score adjustments instead.

### Superlinked Event/Effect — NOT USED
Was discussed as a mechanism for user feedback. Not implemented because we switched away from Superlinked's runtime to pre-computed embeddings.

---

## Architecture (what's actually running)

```
User types query
    │
    ▼
Planner (questioning.py) — LLM asks 0-2 questions
    │  produces: expanded query, weights, annotations
    ▼
Embedding search (numpy cosine similarity on pre-computed embeddings)
    │  5 fields × 466 people, weighted combination
    │  returns top 50
    ▼
Prior scorer (scoring.py) — additive adjustments from profile_features.json + priors.jsonl
    │  returns top 15 re-sorted with score explanations
    ▼
Results shown in Flask web UI
    │
    ├── "Find similar" → re-search using person's embeddings as query
    └── (NO feedback buttons, NO session tracking)
```

## File inventory

```
search_web.py           Flask app, all routes and UI (localhost:5555)
questioning.py          LLM intent planner
scoring.py              Deterministic prior scorer
extract_features.py     Offline feature extraction
eval.py                 Eval harness (metrics computation)
eval_ui.py              Eval ranking UI (localhost:5556)
search.py               CLI search tool (original, still works)

search_data.json        466 applicants (from Airtable)
search_embeddings.npz   Pre-computed embeddings (5 fields × 466)
profile_features.json   Derived features (seniority, practitioner, orgs)
priors.jsonl            7 global priors (seed)
eval_draft.json         20 eval queries with top-15 results
eval_set.jsonl          9 hand-ranked queries with reasons

enrich_linkedin.py      EnrichLayer bulk enrichment script
fetch_applicants.py     Airtable data puller
```

## Known issues

1. **No feedback mechanism in the search UI.** Spec calls for it, not built.
2. **No duplicate merging.** Some people appear multiple times.
3. **Feature extraction overcounts.** Government experience (299/398) and seniority (260/398 executive) are inflated by broad keyword matching.
4. **Priors are net-negative** on current eval (-0.015 NDCG). The generic `government_for_advisory` boost hurts queries where think tank/academic backgrounds are more relevant.
5. **Superlinked not used at runtime.** Switched to raw numpy for instant startup (~2s vs 10+ min). Superlinked is installed but only used for the schema/space API pattern reference.
6. **Planner requires `ANTHROPIC_API_KEY` environment variable.** Falls back to direct search without it.
