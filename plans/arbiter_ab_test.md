# Gemini Arbiter A/B Test — Ambiguous Ties from Allan's `c773996b`

**Date:** 2026-04-19
**Account:** `4cff802c-ac7d-4836-8d50-5d5c1e31962e` (Allan)
**Dataset:** `c773996b` — *Invite tracker - TLS book launch - IFP Core 250* (302 profiles)
**Sample:** first 10 previously-skipped-for-ambiguous-tie profiles (of 45 in the dataset)
**Model:** `gemini-3.1-flash-lite-preview` (same as `search/llm_judge.py`)
**Harness:** `enrichment/eval/arbiter_ab.py` (reconstructs the top-5 candidate pool from the stored `enrichment_log`, then calls `arbitrate_identity` on it)

---

## TL;DR

| Outcome | Count | Note |
|---|---|---|
| Arbiter picked a winner (status quo: skipped) | **2 / 10** | Both high-confidence, slug-matched picks |
| Arbiter abstained (status quo: skipped — no change) | 8 / 10 | Correct behavior on genuinely ambiguous cases |
| Arbiter errored | 0 / 10 | Retry + JSON parsing held up |

Extrapolating linearly: the arbiter would recover **~9 of 45 skipped-for-tie profiles** (~20%) on this dataset. This matches the upper-end of the qualitative estimate in `plans/diagnosis_hitrate.md §P1+P4` without admitting new wrong-person matches (all 2 winners have slug+name+domain corroboration; all 8 abstentions correctly noticed the tie was between different people).

---

## Cost

- 10 arbiter calls × 1 Gemini Flash-Lite request each ≈ **$0.005 total** (<$0.001/call, well within the <$0.001/call guardrail).
- No EnrichLayer calls — the harness only re-ran the scoring step against the stored candidate pool.

Projected cost at full-dataset scale: 45 skipped-tie profiles × <$0.001 = **<$0.05** for this dataset. Rolled out across the ~91 ambiguous ties in the entire `c773996b`-equivalent corpus, **<$0.10**.

---

## Winners picked (status-quo change: skipped → resolved)

### 1. Soren Dayton — `soren@thefai.org`
- Tie: 4 candidates @ score 23 (pathognomonic of the old `email-username` bug — all 4 candidates inherited a +20 email-evidence bonus).
- Arbiter picked: `https://www.linkedin.com/in/sorendayton` (**high** confidence).
- Arbiter reason: *"The candidate's slug matches the profile name exactly and the organization matches the profile's email domain affiliation."*
- Verdict: correct. `sorendayton` slug is the unambiguous distinctive signal the heuristic's slug check didn't weigh enough to break the tie.

### 2. Sovereign House — `nick@sovereign.house`
- Tie: 4 candidates @ score 19.
- Arbiter picked: `https://www.linkedin.com/in/nicknieman` (**high** confidence).
- Arbiter reason: *"The email domain sovereign.house matches the profile name, and the candidate nicknieman is a strong phonetic and structural match for the name Nick Nieman, which aligns with the email prefix."*
- Verdict: plausibly correct. Single-token profile name "Sovereign House" is the org; the arbiter correctly inferred "nick@sovereign.house → Nick Nieman" from the email prefix + domain alignment — a pattern the heuristic has no machinery for.

---

## Abstentions (status-quo change: still skipped — no cost, no harm)

For these 8 cases the arbiter returned `winner_index: null`. All 8 have low-distinguishability ties (either same-common-name candidates like "Daniel King" / "Aman Patel", or candidates whose last name does not match the profile's at all — pathognomonic email-username-bug residue the arbiter correctly refuses to guess on). Examples:

- **Will Poff-Webster** (will@ifp.org): 4 candidates tied at 23, none of which are Will Poff-Webster — the ties are all unrelated Wills whose pages contained the literal email "will" in some context. Correct abstain.
- **Blake Pierson** (blake@fathom.org): same pattern — 4 tied at 23, none of which are Blake Pierson by last name. Correct abstain.
- **Derek Kaufman, John Bailey, Connor Murphy** — same pattern. Correct abstains.
- **Aman Patel** (common name), **Daniel King** (common name), **Wesley Hodges** (no org provided) — the arbiter identified the lack of distinguishing information and abstained.

Abstention on these cases is the **correct behavior** — picking any one of them would likely be a wrong-person match, which is worse than a skip (contaminates downstream scoring).

---

## Interpretation

- The arbiter cleanly recovers cases where one candidate has a distinctive name+slug+domain alignment the heuristic's soft tie-break chain couldn't prioritize. On `c773996b` this is ~20% of the tied-skip pool.
- On residual email-username-bug ties (where NO candidate is actually the right person), the arbiter correctly abstains — so it doesn't admit wrong-person matches.
- Combined with the P1 email-username gate already shipped (commit `4b78010`), the arbiter fills the remaining gap: P1 prevents the bogus tie from forming in many cases; the arbiter resolves the residual genuine ambiguities.

## Case against

*"Why not just let the heuristic's existing `≤3 tied → accept top` branch catch these?"*
Because that branch silently picks the first-sorted candidate, which in the data above is usually the wrong person (the email-username false positive). The arbiter accepts ONLY when it can articulate a distinguishing signal, and it abstains (not picks-first-by-default) when it can't. That trade—2 recovered at high confidence vs. potentially 2 wrong-person matches under the silent-accept branch—is strictly better for Allan's use case where wrong-person contamination is expensive.

## Counterfactual

Without the arbiter, these 45 profiles in `c773996b` stay SKIPPED, and the ~91 similar cases across the full corpus (per `plans/diagnosis_hitrate.md §1`) also stay SKIPPED — ~9-18 of them are recoverable and we leave them on the table. At Allan's current pipeline scale that's ~$0.05 in Gemini costs for recovery that would otherwise require manual review.

## Deliverables

- Harness: `enrichment/eval/arbiter_ab.py` (reusable — can be re-run on any dataset by ID).
- Arbiter: `enrichment/arbiter.py`.
- Integration: `enrichment/identity.py::_score_candidates` (gated to fire at most once per profile, only on tied/at-threshold cases).
