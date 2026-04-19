# Candidate Search — Correctness & Failure-Mode Diagnosis

**Author:** correctness audit, cross-checked with Codex.
**Dataset sampled:** Allan's `c773996b`, 302 profiles, ran 2026-04-19 ~14:00 EDT.
**Tally:** 184 enriched, 91 skipped, 26 failed, 1 pending.

## Headline finding
**~32 of 184 enriched profiles (~17%) are matched to the wrong person.** Evidence: profile's distinct last name (≥4 chars) is entirely absent from the enriched LinkedIn's `full_name`. Downstream damage: `enrichers.py:117-123` backfills the wrong person's `current_company` and `current_title` into `profile.organization` / `profile.title`, then the profile card shown to the LLM judge presents the wrong person's bio as ground truth. Examples: `Joshua New` (seedai.org) scored as "Founding Partner at Lux Research"; `Evan McVail` scored as "Engineering Program Manager at Orlando Health"; `Dan Turner-Evans` and `Dan Lips` BOTH attached to the same `Dan Fragiadakis PhD` LinkedIn profile.

**Timing caveat:** the `last-name-missing` weak-match guard at `enrichers.py:190-203` was committed at 14:16 EDT (commit `8a42905`), shortly after Allan's run finished. That fix alone likely cuts the 32 wrong-person cases to ~8-12. The remaining recommendations apply after that fix.

---

## Top 5 failure modes

### FM1 — `email-username` LinkedIn hits get `_email_evidence = True` (the actual root cause)
**Frequency:** drives most of FM3 (13 duplicate-URL assignments) AND most of FM5 (≥30 of 45 ambiguous skips show ties at `score=23 = 20+2+2-1`).

**Root cause:** `identity.py:527` runs a broad query:
```python
broad_email = search(f'"{email_local}" linkedin OR researchgate', "email-username")
```
For common email locals (`dan`, `ari`, `evan`, `josh`, `allan`), this returns a mix of LinkedIn profiles belonging to *unrelated people whose pages mention "dan"*. Then `identity.py:558-561`:
```python
if _is_linkedin_profile_url(r.get("url", "")):
    r["_email_evidence"] = True
    all_candidates.append(r)
```
labels ALL of them as email-evidence → they get `+20` at `identity.py:710` in `_score_candidates`. A generic "dan" search on LinkedIn returns 5 unrelated Dans, all at score 23.

**Evidence in logs:**
- `Will Poff-Webster` (will@ifp.org): 4 candidates tied at score 23 → skipped.
- `Connor Murphy`, `Soren Dayton`, `Blake Pierson`: all 4-5 way ties at 23.
- `dan@ifp.org` and `dan@thefai.org` point to the same wrong LinkedIn (broker aggregation + email-username mislabeling compound).

**Also:** `_follow_email_evidence` at `identity.py:329` only runs on `email_results` (the literal `"<email>"` query), not on `email-username`. So Codex's observation stands: the broker HTML-fetch path is narrower than the draft implied. The real bleed is the `email-username` label.

### FM2 — Org mismatch is only `-1` vs name-match `+2` / `+3` (too soft)
**Frequency:** 27 enriched with `ACCEPTED (score=2, checks=1)` — name match + explicit org mismatch.

**Root cause:** `enrichers.py:256-258`:
```python
else:
    score -= 1
    log.append(f"  Verify org: MISMATCH ...")
```
Strong name match (both first+last, ≥10 chars total → `score += 3` at line 209) plus org mismatch (`-1`) nets `+2`, which clears threshold at `enrichers.py:330`. Same-name-different-person is the typical case.

**Evidence:**
- `Abigail Olvera` (Dept of State) → `Abigail Olvera` property manager at MegaCorp Logistics. Perfect name match, org MISMATCH (Dept of State ≠ MegaCorp), still accepted.
- `Connor O'Brien` (EIG) → `connorobrien11` Account Exec at Kevel.
- `Thomas Kelly` (Horizon Fellow) → Owner of Creative Soulz Printing.
- `Trevor Levin` (Open Philanthropy) → Real Estate at Nourmand & Associates.

### FM3 — Bare name match passes when org is vague or missing (no corroborator required)
**Frequency:** 47 enriched with `ACCEPTED (score=2, checks=0)`. Of these, ~32 have a different last name in the enriched profile (mostly caught by the new guard, but some escape when the profile name is one token or abbreviated).

**Root cause:** `enrichers.py:259-260` skips the org check when the org is vague/missing:
```python
elif org_is_vague:
    log.append(f"  Verify org: SKIPPED (vague org: '{profile_org}')")
```
Then name-only match scores `+2` and passes threshold at line 330. No secondary signal required.

**Evidence (even with new guard in place):**
- `Ben Reinhardt` (Self-employed → vague) → `Ben Sigelman` (different person, also self-employed).
- `Allan Buchness` (no org) → `allanjabri` — LinkedIn hides last name as "Allan J."; guard may or may not fire depending on whether "buchness" substring-matches (it doesn't).
- `Nat Purser` (no org) → `Nat Robinson`.

### FM4 — Tie-breaking accepts 2-3 tied candidates as "low confidence"
**Frequency:** affects unknown subset of the 45 ambiguous rejects (which it DOESN'T catch) and some of the wrong-person accepts (which it DID catch — pick the top tied one).

**Root cause:** `identity.py:854-862`:
```python
elif len(tied) <= 3:
    log.append(f"  {len(tied)} candidates tied at score {best_score} — accepting top with low confidence")
else:
    log.append(f"  REJECTED: {len(tied)} candidates tied ...")
```
When ≤3 candidates tie, the first one (sort is not deterministic across candidates tied at same score) is silently accepted. This is a coin-flip in disguise.

### FM5 — Contaminated backfill pollutes downstream scoring
**Frequency:** every wrong-person acceptance (32 in this run) ALSO contaminates the profile card. Even if we're fine with LinkedIn data being "wrong but flagged", the scoring path currently treats it as clean input.

**Root cause:** `enrichers.py:117-123` backfills `profile.organization`, `profile.title`, `profile.name` from enriched data (only when missing). Then `summarizer.py:147-172` assembles the profile card and places `title at organization` as the header line given to the LLM scorer. The scorer has no way to know this is unverified.

**Evidence:** `Joshua New` card reads "Founding Partner & Managing Director at Lux Research" as a fact.

### FM6 — Legitimate API 404 on correct LinkedIn URL treated as wrong-person
**Frequency:** 5 / 26 failures (Daniel Schory, Cody Fenwick, Ashley Nagel, etc.).

**Root cause:** `enrichers.py:470-484` returns `None` on 404 identically to any other API failure. Then `enrichers.py:99-103` treats `None` as "not this URL" and falls through to the next alternative. High-confidence correctly-identified URLs (score ≥ 10) get silently dropped.

Also: `Ashley Nagel` shows a single URL retried twice (with/without trailing slash) because `urls_to_try` isn't de-duped after normalization.

---

## Proposals (ranked, post-critique)

### P1 (ship first) — Stop labeling email-username LinkedIn hits as `_email_evidence`; tighten email-evidence scoring
**File:** `enrichment/identity.py`, lines 527, 529-561, 710. Also scoring block at 808-863.

**Changes:**
1. At line 527, KEEP the search but CHANGE the label from `"email-username"` to `"name-or-username-broad"` AND do NOT auto-tag its LinkedIn results with `_email_evidence=True`. Only literal-email-match results (from the `"{email}"` query at line 523) qualify as email evidence.
2. In the loop at lines 529-561, split paths:
   - For `email_results` (literal email): keep `+20` for extracted-from-snippet LinkedIn URLs (genuine ground truth).
   - For `broad_email`: snippet extraction still happens but score those LinkedIns at `+5` as "weak-email-proximity", and require an independent corroborator (slug match, org match, etc.) in `_score_candidates`.
3. In `_score_candidates` at line 710, change:
   ```python
   if c.get("_email_evidence"):
       score += 20
   ```
   to differentiate:
   ```python
   et = c.get("_email_evidence_type")
   if et == "exact":
       score += 20
   elif et == "proximity":
       score += 5
   ```
4. Tie-acceptance at line 854-856: remove the `≤3 tied → accept top` branch. Any tie at an email-proximity score must be resolved by a distinguishing signal OR rejected.

**Expected impact:** eliminates the 13 duplicate-URL assignments; converts many of the 45 ambiguous skips into either correct resolutions (when slug/org exists to break the tie) or clean no-candidate rejects (when we genuinely can't tell).

**Risk:** loses some correct matches that rely only on broker-snippet LinkedIn attribution (probably <5% of current corpus based on spot checks). Acceptable trade.

### P2 (ship second) — Non-vague org mismatch requires a strong secondary positive to accept
**File:** `enrichment/enrichers.py`, lines 256-261, 330-335.

**Change:** Track which positive signals fired. When org mismatch is observed AND the org is NOT vague:
- Require at least one strong positive besides name: email-domain appears in experience companies, title partial match ≥60%, strong content-relevance match (`≥3 shared terms` at existing line 319), or `content-relevance: WEAK` did NOT fire.
- If no strong positive fires alongside the name, reject even when name match is "strong".
- Keep soft-penalty behavior for vague orgs (where mismatch shouldn't be a hard fail — OpenPhil → Coefficient Giving is a real rename case).

**Expected impact:** eliminates the 27 same-name-different-person cases (FM2). Keeps legitimate renames/transitions because those usually match via shared experience entries.

**Risk:** rejects legitimate job transitions where the person's LinkedIn hasn't been updated AND experience history doesn't yet reflect the new role. Mitigation: soft-fail them into `skipped` not `failed`, so they surface in a "needs manual review" queue.

### P3 (ship alongside P2) — Decontaminate profile card from unverified enrichment
**File:** `enrichment/enrichers.py` lines 117-123 AND `enrichment/summarizer.py` build_profile_card (lines 147-172).

**Change:**
1. Add a field `Profile.enrichment_confidence` ∈ `{"high", "medium", "low"}` populated from the verify score tier.
2. Only backfill `profile.organization` / `profile.title` when `enrichment_confidence >= "medium"` AND at least one `check` fired positively (not just name match).
3. In `summarizer.py`, when confidence is "low", prepend the header with a flag: `"[UNVERIFIED LINKEDIN]"` AND label the `context_block` prefix accordingly. This signals the LLM judge to discount structured LinkedIn claims and rely on user-provided content fields.
4. Per Codex's critique: `profile.linkedin_enriched` is ALSO a contamination vector — consider storing the raw enriched data under `profile.linkedin_enriched_raw` and only promoting it to `linkedin_enriched` if confidence is high.

**Expected impact:** even when FM2/FM3 fail, downstream scoring isn't poisoned. LLM judge is told "this LinkedIn attachment is weak" and can weight accordingly.

**Risk:** requires plumbing a new field through summarizer + scoring. Non-trivial but contained.

### P4 (ship after P1-P3) — Tighten `_verify_match` with slug awareness
**File:** `enrichment/enrichers.py` — requires passing the LinkedIn URL into `_verify_match` (currently signature at line 137 doesn't take it).

**Changes:**
1. Extend signature: `_verify_match(profile, enriched, linkedin_url)`.
2. In the existing `last_name_missing` check at lines 190-203, also check the URL slug. Structure:
   - Compute `slug = linkedin_url.rstrip("/").split("/")[-1].lower().replace("-", "").replace("_", "")`.
   - If profile has first+last AND profile_last ≥ 3 chars AND profile_last NOT in enriched_name AND profile_last NOT in slug: REJECT.
   - Conversely, if enriched_name is abbreviated ("Ari K.") but slug contains full first+last (`ari-kagan-...`), DO allow match even though `last_name_missing` would otherwise fire.
3. Add a single-token profile handler: when `len(profile_parts) == 1`, require slug to contain the profile name OR email-local to match slug, else reject.

**Expected impact:** catches the remaining ~5-10 wrong-person cases that slip past the current guard. Enables correct handling of legitimate "LinkedIn hides last name for privacy" cases (today they'd be rejected by the existing guard, tomorrow they'd be accepted when slug corroborates).

**Risk:** Eastern European / transliterated / hyphenated / name-changed profiles: the guard has no phonetic or transliteration awareness (only Unicode NFD stripping exists). Acceptable because it's a narrow existing class; doesn't regress from status quo.

### P5 (ship as polish) — Split API failures from wrong-person; dedupe URLs; observability flag
**Files:** `enrichment/enrichers.py:470-484` (API), `enrichment/enrichers.py:77-95` (urls_to_try loop), new `Profile.enrichment_flags`.

**Changes:**
1. In `_call_api`, return a tagged result: `("ok", data)`, `("404", None)`, `("transient", None)` so the caller can distinguish.
2. In `enrich_profile`, dedupe `urls_to_try` after normalization (Ashley Nagel bug).
3. On 404 for a high-confidence URL (identity resolution score ≥ 10), set `enrichment_status = "api_404"` distinct from `"failed"`. Retain the URL on the profile so manual re-enrichment can retry it later.
4. Add `Profile.enrichment_flags: list[str]` populated with `"suspect-name"`, `"suspect-org"`, `"duplicate-url"` (computed post-batch), `"api-404"`. Surface this count in dataset stats and the UI.

**Expected impact:** 5 recoverable profiles in Allan's run. Ongoing observability. Enables targeted re-runs.

**Risk:** Minimal.

---

## Proposals NOT ranked (explicitly deprioritized)

- **LLM-based final verification.** Codex is right — this is premature. Today the primary failure is permissive deterministic thresholds and mis-typed evidence. LLM verifier becomes useful AFTER P1-P4 as a last-pass adjudicator for narrow borderline cases (abbreviated names with slug support, strong-name + org-mismatch renames). Adds ~$0.001 × N to cost, significantly slower, and harder to test. Revisit in v3.
- **Draft's P1 as originally written** (last-name guard in `_verify_match` with just substring check): superseded by this P4, which requires slug passing. Don't ship the simpler version standalone; it creates false-negatives for legitimate abbreviation cases without the slug escape hatch.
- **Draft's P3 as originally written** (distrust broker domains specifically): Codex showed brokers aren't the main failure path — `email-username` labeling is. Shipping P1 (above) subsumes P3 benefits without the false-negative hit on legitimate broker hits.

---

## Test plan

### Retrospective validation (no re-API cost)
1. Load Allan's dataset, re-run `_verify_match` in-memory with new logic against stored `linkedin_enriched` + `enrichment_log`. Count:
   - How many previously-accepted become rejected (target: 25-30 wrong-person cases flipped).
   - How many previously-accepted remain accepted (target: ~150 retain).
   - How many previously-skipped (ambiguous) become resolvable (target: ≥10 recovered).

### Hand-labeled gold set
Create `enrichment/test_fixtures/gold_set.json` with 50 profiles hand-verified (30 correct enrichments, 10 wrong-person, 10 thin-profile edge cases including abbreviated names). Run integration test that computes precision/recall on each proposal.

### Specific regression fixtures
Small focused JSON files in `enrichment/test_fixtures/`:
- `p1_email_evidence.json` — 5 cases where broad email-username query returns wrong LinkedIns (should no longer score as evidence).
- `p2_org_mismatch.json` — 5 same-name-different-company cases (should reject).
- `p2_legitimate_rename.json` — 3 real job transitions (should accept, vague-org branch).
- `p4_abbreviated_match.json` — 5 "Ari K." / "Allan J." style with strong slug (should accept with slug check).
- `p4_single_token.json` — 3 one-word profile names (should reject without corroborator, accept with slug/email match).

### Unit tests
- `test_verify_match.py` — exhaustive accept/reject table. Include: diacritics, name-change, hyphenation, apostrophes, `Ph.D.` suffixes, initial patterns, compound surnames.
- `test_identity_resolver_scoring.py` — assert email-evidence tier correctly differentiated; tie-handling rejects `≤3 tied` at high scores.

### Bump `ENRICHMENT_VERSION` to `v2`
`pipeline.py:46` — so the UI can show "re-enrich available" for v1-stamped profiles.

### Manual spot-check
After deploying, run 20 random Allan profiles against new pipeline; hand-Google them; report precision and recall.

---

## Risks & open questions

- **Precision / recall trade:** P1 + P2 together significantly tighten precision. Accept rate likely drops from 61% (184/302) to ~52-55%. Allan's usage probably prefers "lose 20 correct ones" over "keep 20 wrong ones" because wrong-person contamination silently corrupts scoring. Confirm with Jonah before shipping.
- **Sort non-determinism at ties:** `sorted()` in Python is stable, but the input order of `all_candidates` depends on web search result order which can vary between Brave/Serper and across runs. If we keep any tie-acceptance path, we need explicit tie-break by slug quality.
- **Broker usefulness:** Brokers ARE useful when they correctly map email → LinkedIn (maybe ~50% of the time per eyeballed sample). P1 downgrades them to "proximity evidence" with `+5` rather than rejecting them outright — this preserves the signal but prevents solo decisions on broker data.
- **No ground truth:** the hand-labeled gold set is a one-time investment (~30 min) that unblocks regression testing for this + future changes. Should be built first.
- **Single-name profiles:** "Beez Africa", "Abie R" — ~5% of dataset. P4's single-token handler is required, not optional.
- **Version tagging:** P3 requires a new field on `Profile`. Need migration path for existing `profiles` rows in Supabase (likely: derive `enrichment_confidence` on the fly from stored `enrichment_log` for backfill).

## Summary of what to ship

1. **P1** — fix email-evidence labeling + remove ≤3-tied auto-accept. Addresses FM1/4, biggest wins.
2. **P2** — require strong positive when org mismatches. Addresses FM2.
3. **P3** — decontaminate profile card. Addresses FM5 (downstream blast radius).
4. **P4** — slug-aware verify. Addresses remaining FM3 post-guard.
5. **P5** — observability + API 404 handling. Polish.

P1-P3 together are the MVP. P4-P5 are follow-ups.
