# TLS Applicant Search — Project Context

## What we're building

A semantic search tool over ~466 TLS (The Launch Sequence) applicants. Each applicant has: a one-sentence pitch, a problem statement, a proposed solution, uncertainties, a scraped LinkedIn profile (experience, education, headline), and Claude's author assessment from the review pipeline. The goal is to type a natural language query like "biosecurity expert with government experience" and get back the right people, ranked well.

The longer-term ambition is a product that could serve multiple use cases: (1) an open-source tool where someone feeds in a list of emails, enriches them to LinkedIn profiles, and gets semantic search over their own contacts, and (2) a database search layer that organizations plug into their existing people databases. Possibly federated across organizations eventually.

## What exists now

- **Data pipeline**: 466 applicants pulled from Airtable. 342 have enriched LinkedIn profiles (via EnrichLayer API — full experience, education, about sections). All have pitch/problem/solution text and Claude's author assessment.
- **Working search tool**: A Flask web app (`search_web.py`) at localhost:5555, powered by Superlinked. Five text embedding spaces (pitch, problem, solution, LinkedIn profile, author assessment) plus one categorical space (category). Uses `sentence-transformers/all-mpnet-base-v2` for embeddings. Dynamic weight sliders in the UI. Click-to-expand results showing full profile details.
- **Superlinked integration**: Using the open-source framework for multi-aspect embeddings with per-field weighting adjustable at query time. Schema, spaces, index, and query are all defined using Superlinked's API.

## What we want to add

### 1. Progressive questioning (before search)
Instead of just searching on the raw query, the system should ask 1-2 clarifying questions to understand what the user actually needs. Research shows a single well-chosen question improves precision@1 by ~170% (SIGIR 2019). The highest-value question is about purpose ("what do you need this person for?"), not topic. The librarian reference interview and expert network intake process (AlphaSights, GLG) are the models here.

### 2. In-session feedback ("find more like this")
After seeing results, the user should be able to point at a result and say "more like this person." Superlinked has `with_vector()` that natively supports this — search using a person's embedding as the query vector instead of text.

### 3. Global priors that improve search over time
Persistent domain knowledge that should influence all searches. Examples: "RAND is democrat-coded," "practitioners who've shipped things are more valuable than researchers who've published," "a PhD student is less credible than a DOE program manager for policy work." These should accumulate over time as the user provides feedback, making the system smarter for everyone.

## The core technical tension

The embedding model (a frozen sentence transformer) computes similarity geometrically. It doesn't understand editorial judgments. "RAND is democrat-coded" is a fact about the world that cosine similarity cannot learn. The question is where and how to inject domain knowledge into the search pipeline.

### Options explored

**LLM re-ranking** (retrieve top N, have an LLM re-rank with priors as context): Research shows this can actually degrade results when retrieval is already decent (Voyage AI, Oct 2025). LLMs have position bias, hallucinate rationales, and produce inconsistent scores. Cross-encoder re-rankers consistently outperform LLMs on this task.

**Instruction-following cross-encoders** (pass priors as natural language instructions to a cross-encoder that re-scores query+candidate pairs): More promising than LLM re-ranking. The cross-encoder can jointly reason about the query, the candidate, and the priors. But this is still re-ranking — it happens after retrieval.

**Multiplicative score boosting** (encode priors as numeric signals per profile, multiply into similarity scores): Simplest, deterministic, no API cost. `final_score = cosine_sim * ln(1 + weight * signal)`. But requires pre-computing numeric features per profile.

**Fine-tuning the embedding model**: Would make the model better at matching queries to profiles over time. But requires hundreds to thousands of labeled (query, person, relevant/not-relevant) pairs, which don't exist yet. Also can't teach entity-level facts ("RAND is democrat-coded") — only general matching patterns. Every fine-tune invalidates all cached embeddings. Risk of catastrophic forgetting on small datasets.

**Superlinked's Event/Effect mechanism**: Built-in feedback loop. User interactions (thumbs up/down) create events that shift the user's search vector over time. Configurable event_influence (how much feedback overrides stated preferences) and temperature (recency bias). But this is per-user personalization, not global priors.

**Injecting priors into query construction**: The LLM that does progressive questioning can also read a global priors file and adjust Superlinked's dynamic weights accordingly. "Weight operational experience over academic credentials" → set linkedin_weight=0.5, problem_weight=0.1. This shapes the search itself, not a re-ranking step. Works for weight-level priors but not for entity-level judgments.

### Where things stand

The user wants global priors to go into the search itself, not be applied as a separate re-ranking step. For weight-level priors ("care more about experience than pitch topic"), this is achievable by having the query construction step read priors and adjust Superlinked weights. For entity-level priors ("RAND is democrat-coded"), there's no way to make the embedding model understand this — some form of score modification is unavoidable, even if it's integrated to feel like part of the search rather than a separate step.

The open question is the right architecture for combining: (a) progressive questioning, (b) dynamic weight adjustment from priors, (c) entity-level score modifiers, and (d) in-session feedback — in a way that feels like one coherent search experience rather than a pipeline of separate steps.

## Superlinked features being used vs. available

| Feature | Using? | Notes |
|---------|--------|-------|
| TextSimilaritySpace (multi-field) | Yes | 5 text spaces with independent weights |
| CategoricalSimilaritySpace | Yes | For TLS category |
| Dynamic Param weights | Yes | Adjustable at query time via sliders |
| NumberSpace | No | Could use for years of experience if extracted |
| RecencySpace | No | Could use for application date |
| Natural Language Querying (NLQ) | No | LLM auto-extracts weights/filters from query text |
| EventSchema + Effect | No | Built-in feedback loop for user personalization |
| with_vector() | No | "Find more like this person" |
| Hard filtering (.filter()) | No | Structured filters on category, etc. |
| CustomSpace | No | Could encode custom priors as a space |

## Key research findings

- Cross-encoder re-rankers beat LLM re-rankers on accuracy, latency, and cost (Voyage AI, ZeroEntropy, Naver Labs)
- One good clarifying question before search improves precision@1 by ~170% (SIGIR 2019)
- The librarian's most valuable question is about purpose, not topic (Taylor 1968)
- E-commerce uses multiplicative boosting for business rules on top of relevance (Google, Elasticsearch)
- Expert networks (AlphaSights) decompose briefs into: industry, value chain position, geography, time period, decision context
- Superlinked's Event/Effect mechanism shifts user vectors based on feedback events, with configurable influence and recency
- Fine-tuning embeddings requires labeled data that doesn't exist yet; entity-level facts can't be learned from small datasets
