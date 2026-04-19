# Hit-Rate Diagnosis & Cost Optimization Plan

Angle: quantitative analysis of enrichment pipeline performance. Backed by supabase data from 9,814 profiles across IFP, Allan, BlueDot accounts (pulled 2026-04-19).

---

## TL;DR

- 68.2% of **processed** profiles enrich successfully (877/1285). 86.9% of the overall pool (8,529/9,814) is pending — mostly Allan's three unprocessed datasets.
- Hit rate on profiles that **need identity search** (no preset LinkedIn URL): **56.2%** (200/356). This is the number to optimize.
- EnrichLayer is **97% of spend** ($15.91 of $16.40 for BlueDot's full 684-profile run). Search cost is trivial. Optimize verification, not search.
- Biggest single loss surface: **91 ambiguous ties at identical scores** (47% of skipped) driven by email-username heuristic giving the same +20 bonus to multiple wildly-different LinkedIn profiles when the email local-part is a common word.
- Second biggest: **72 of 216 failures** (33%) had name match + org-mismatch → rejected below threshold, even when the person had just changed jobs.
- Third: **dead code** — `_follow_email_evidence()` never runs because `search()` filters out non-LinkedIn URLs before they can be followed.

---

## 1. Data-backed hit-rate table

### Processed only (n=1,285; pending excluded)

| Segment | N | Enriched | Skipped | Failed |
|---|---|---|---|---|
| ALL processed | 1,285 | 68% | 15% | 17% |
| preset LinkedIn URL | 929 | 73% | 9% | 19% |
| needed identity search | 356 | **56%** | **32%** | **12%** |

### Input quality × outcome (search-needed profiles only, n=356)

| Input quality | N | Enriched | Skipped | Failed |
|---|---|---|---|---|
| name + corp-email + org | 174 | **70%** | 20% | 10% |
| name + personal-email + org | 124 | 60% | 25% | 15% |
| name + email (no org) | 57 | **5%** | **82%** | 12% |
| no email | 1 | — | — | — |

**Reading**: organization is the single most predictive input. Corporate email is worth ~10 points over personal. Email-only profiles (name + email without org) collapse to 5% — they're the hardest case.

### Per-account

| Account | Processed | Enriched | Failed | Skipped | Notes |
|---|---|---|---|---|---|
| IFP | 300 | 97% | 0.3% | 2% | 97% had preset LinkedIn (upstream already resolved it) |
| Allan (ds `c773996b`) | 302 | 61% | 9% | 30% | Only one of Allan's 4 datasets processed |
| BlueDot | 684 | 59% | 28% | 14% | Fully processed; highest failure rate |

**BlueDot** is the canonical "finished run" — its 59% enriched / 28% failed / 14% skipped is the most honest benchmark for what the pipeline produces end-to-end.

---

## 2. Where do processed profiles die?

| Stage | Count | % of processed |
|---|---|---|
| 01 Enriched | 877 | 68.2% |
| 02 No logs (silent skip) | 52 | 4.0% |
| 04 No LinkedIn found after all searches | 44 | 3.4% |
| 05 Score below threshold | 5 | 0.4% |
| 06 Ambiguous (tied at top) | **91** | **7.1%** |
| 07 All candidates failed verification | **216** | **16.8%** |

The two dominant loss surfaces:

**Ambiguous ties (91 profiles)**
- Of 91 tie distributions: 29 tied at score 23, 20 at 12, 14 at 9. The 23-ties are pathognomonic of email-username bonus bug.
- Example: Milica Cosic (eig.org) — 4 random "milica-*" profiles tied at 23 (all +20 email-evidence, +2 first-in-title, +2 slug-name, −1 slug-no-last), while the correct `milicacosic/` slug sits at score 9 (no email-evidence bonus).
- 58 of 91 (64%) ambiguous cases have a candidate whose slug contains both first AND last name — strong implicit correct answer.

**Verification failures (216 profiles)**
- 208 tried 1 URL, 28 tried 2, 5 tried 3+, 1 tried 17.
- Attempt-level reject reasons (within `_verify_match`): score_insufficient 174, org_mismatch 121, name_mismatch 30.
- Profile-level "name matched + org mismatched → rejected below threshold": **72 of 216 (33%)**.
  - Note: org mismatch is already a soft penalty (−1), not a hard reject (per enrichers.py:257). The rejection comes from the cumulative threshold of score ≥ 2, where name-match alone (+2) is exactly at threshold and a single penalty drops it to 1.

---

## 3. Which search strategies actually produce wins?

Among the 189 search-needed profiles with a traceable winning URL:

| Winner label | Count | % |
|---|---|---|
| email-username | 169 | 89.4% |
| slug-email | 18 | 9.5% |
| email-exact | 2 | 1.1% |
| all other 10 labels combined | 0 | 0% |

**Caveat** (per Codex review): attribution is biased. Searches run in order; URLs deduplicated; first-touch wins. `email-username` runs second (right after email-exact), `slug-email` runs late. Marginal contribution ≠ first-touch attribution. The true insight is **name+org, name+title, name+keywords, name+location almost never produce novel URLs a later step wouldn't have caught**. An A/B test disabling them would tell us their marginal value.

---

## 4. Cost analysis

### BlueDot full run (684 profiles)
- Search API calls: 123 at ~$0.004 each = **$0.49**
- EnrichLayer calls: 947 at $0.0168 each = **$15.91**
- Total: $16.40, $0.041 per enriched profile
- Wasted (on failed/skipped): 302 EnrichLayer + ~50 searches = **$5.07**

### Cross-account (processed = 1,285)
- Total EnrichLayer calls: 1,148 ($19.29)
- Of those, 302 went to profiles that ended up failed/skipped ($5.07 wasted, 26% of EnrichLayer spend)
- Search queries: ~925 ($3.70 total)

### Cost per stage
| Stage | Cost/profile | Wastage |
|---|---|---|
| Search API | $0.001–$0.016 | negligible |
| EnrichLayer verify (avg 0.89 calls) | $0.015 | 26% on failed/skipped |
| EnrichLayer retries on verify-failures | 1.17 calls avg | 100% waste |

**Conclusion**: search optimization saves pennies. **Verification quality is the lever**. Every percentage point reduction in verify-rejection saves ~$0.20–$0.30 of direct EnrichLayer spend per 100 profiles, plus the enrichment yield.

---

## 5. Ranked proposals

*Effort estimates assume a single-afternoon fix. Lifts are ranges because projections double-count overlap between proposals.*

### P1. Separate `email-exact` evidence from `email-username` heuristic (highest impact)

**What's broken.** Both search labels currently mark their LinkedIn results as `_email_evidence = True`, earning +20 in scoring (identity.py:529, :708). But only `email-exact` is true ground-truth (pages containing the literal email). `email-username` just queries `"<local_part>" linkedin OR researchgate` — for common locals like `john`, `milica`, `colin`, this returns multiple unrelated profiles that all get +20.

**Evidence.**
- 91 ambiguous rejections (7.1% of processed). 29 tied at the exact pathognomonic score 23 = +20 email-evidence + 2 first-in-title + 2 slug-name − 1 slug-no-last.
- 58 of 91 (64%) ties have a candidate whose slug contains both first AND last name, but at a lower score because no email-evidence bonus.

**Change.** identity.py:527–561 — remove or gate the `_email_evidence = True` flag for `broad_email` (email-username) results. Either:
  - (a) Drop the +20 bonus entirely for email-username results — rely on the normal slug/name/org signals.
  - (b) Only flag `_email_evidence` when the result's URL was produced by `email-exact` search.

**Expected lift.** ~20–35 recovered profiles from current ambiguous-skip pool. Can't be confirmed without replay — but in the 58 cases where a full-name-slug candidate is already in the pool at score 9, the corrected scoring would make that candidate the unambiguous winner.

**Validation experiment.** Re-score existing candidates pools (no new searches needed) with P1 applied. Compare how many previously-tied cases now have a unique winner. Manually audit 20 to confirm the winner is the right person.

---

### P2. Strengthen name-match baseline before penalizing

**What's broken (corrected framing).** Codex flagged the original claim ("org mismatch is a hard reject") as misstated. Reality: org mismatch is a soft penalty (−1 at enrichers.py:257), but the acceptance threshold is `score >= 2` (enrichers.py:330) and `Verify name: MATCH` awards only +2 for the common case. So any single soft penalty — org mismatch, location mismatch, content-zero-overlap — drops the profile to score 1 → reject.

**Evidence.**
- 72 of 216 failed profiles (33%) had at least one attempt with `Verify name: MATCH` + `Verify org: MISMATCH` and no other positive signal → rejected.
- Many are genuine job changes (Josephine Schwab: profile org "European Institute", LinkedIn "writer"; Florian Aldehoff-Zeidler: profile "AI security consultant / self-employed", LinkedIn "selbstständig" which is literally German for self-employed).
- Some are legitimate wrong-person matches (Joe Kwon: gmail `joekwon333@`, found `connectioncounselor` — common name, no corroboration).

**Change.** enrichers.py:205–215 + :330. Two coordinated adjustments:
  - Boost `Verify name: MATCH` for strong matches: when both first+last match AND the slug also contains both names, award +3 instead of +2. This makes a strong name match survive a single soft penalty.
  - Require at least ONE positive verification signal besides name to cross threshold when there are soft penalties. Current logic accepts "name +2 → score 2" on its own; make this `score >= 2 AND (checks == 0 OR at_least_one_positive_check)`.

**Expected lift.** 20–40 recovered profiles. Overlap with P1 is minimal — P2 targets the 216 verification failures, P1 targets the 91 ambiguous resolutions.

**Validation experiment.** **Offline re-verification.** For the 216 failed profiles, their enrichment_log has the enriched LinkedIn data already fetched. Replay `_verify_match` with P2 logic on that stored data. Count flips from reject→accept. No EnrichLayer calls needed. Manually audit 30 flips for correctness.

---

### P3. Fix the dead email-evidence follow code path

**What's broken.** identity.py:502 — `search()` returns only LinkedIn results (`li_results`). So:
- Line 523 `email_results = search(f'"{email}"', "email-exact")` returns `[]` whenever email-exact finds broker/org pages but no LinkedIn URLs (97 of 316 cases).
- Line 565 `if email_results:` is therefore almost always False, and `_follow_email_evidence()` never runs.
- Same bug hits `org-website` (identity.py:617) and `name+org-broad` (:649) — dead code across three follow-up paths.

**Log-line confirmation.** 0 occurrences of "Following email evidence", "Fetching broker", "Fetching page", "LinkedIn from broker HTML" across all processed profiles.

**Change.** identity.py:502–514 — modify `search()` to return `(li_results, non_li_results)` or have two calls. Feed the `non_li_results` into `_follow_email_evidence()`. Note: this also re-enables org-website scraping (a high-signal ground-truth source for enterprise/academic profiles).

**Expected lift.** Unknown without live test. Codex flagged that this is broader than claimed in the original proposal. Realistic range: 5–20 additional profiles — most valuable for corporate-email profiles where the org website lists team members with LinkedIn links.

**Validation experiment.** Re-run identity resolution for a 50-profile sample of search-needed failures. Compare before/after hit rate. Budget: ~$0.20 additional search cost. Can run in ~2 minutes.

---

### P4. Use full-name slug as tie-breaker

**Evidence.** 58 of 91 ambiguous cases have a candidate with both first + last in slug but at lower score. Codex noted: the code already gives slug-name credit (identity.py:775), so this is a reweighting not a new signal.

**Change.** identity.py:842–863 — when tiebreak fails, add a third tier: if any candidate in the tied pool has slug containing both first+last, promote it as the winner regardless of the tied score. Or: award +5 (up from +4) to candidates with first+last both matching slug.

**Expected lift.** Overlap with P1 is HIGH — Codex correctly flagged this as double-counting. Best to treat P1 and P4 as one combined change. Net combined lift for P1+P4: 25–40 profiles recovered from the 91 ambiguous pool.

---

### P5. Early-reject dead LinkedIn URLs (cost-only, low priority)

**Evidence.** 124 profiles hit `API returned no data` on EnrichLayer (preset URL). Cost: ~$2.08 wasted. 113 of 124 (91%) ended up non-enriched.

**Change.** Pre-check each LinkedIn URL with a HEAD request to `linkedin.com/in/<slug>` before calling EnrichLayer. Skip if 404. Also tighten `is_valid_linkedin_url()` in enrichers.py:26 — it currently accepts `/company/` URLs, but the EnrichLayer endpoint is a profile endpoint.

**Expected impact.** $2–3 cost savings per 1,000 profiles. Zero hit-rate impact.

---

## 6. Combined projected lift (honest estimates)

Starting point for search-needed profiles: **56% hit rate (200/356)**.

| Scenario | Lift range | New hit rate (search-needed) |
|---|---|---|
| P1+P4 combined (de-poison email-username, tie-break with slug) | +20 to +35 | 62–66% |
| P2 (strengthen name-match threshold) | +20 to +40 | 62–67% |
| P1+P2+P4 combined (mostly additive — target different loss pools) | +40 to +65 | 67–75% |
| + P3 (revive dead code) | +5 to +20 more | 68–81% |

**Caveats.**
- Confound: preset=True and preset=False populations are not comparable (different source channels, different failure modes). Improvements to identity resolution don't transfer to preset.
- The "search-needed" segment is only 356 of 1,285 processed profiles. Absolute impact on the full pipeline is smaller: preset=True gains nothing from P1/P4 (they only affect search). P2 helps both segments.
- False-positive risk: loosening verification WILL admit some wrong-person matches. Mandatory: manually audit the first 50 "newly accepted" profiles after any P2 change.

---

## 7. Experiments to run (ordered by cost)

1. **Offline re-verify for P2 (zero cost, ~30 min)**. Replay `_verify_match()` logic on stored enrichment_log data for all 216 failed profiles. Count flips; audit 30.

2. **Offline re-score for P1+P4 (zero cost, ~30 min)**. Walk enrichment_log for each ambiguous-skip profile; extract candidate pool; re-score under new rules; count how many now have unique winners. Audit 20.

3. **Live re-run for P3 (~$0.50, ~5 min)**. Pick 50 search-needed failures, run modified identity resolver, check if new LinkedIn URLs emerge from broker/org-page HTML. Audit any new candidates.

4. **Prospective experiment on pending queue**. Split Allan's 6,392 pending profiles (randomized within account) into control vs P1+P2+P4 treatment. Primary metric: verified enrichment rate. Guardrail: manually-audited wrong-person rate on newly-accepted profiles ≤ 5%. Budget: 500 profiles × $0.04 = $20.

5. **Preset-URL quality audit**. Stratified sample of the 124 "API returned no data" cases. Label each as (a) stale profile URL, (b) /company/ page incorrectly classified, (c) private LinkedIn, (d) malformed URL, (e) vendor miss. This determines whether the 28% BlueDot preset-failure rate is upstream data or pipeline validator.

---

## 8. Top 3 specific code changes

### Change 1: identity.py:527–561
```python
# Before: email-username results get _email_evidence +20 bonus (same as email-exact)
# After: only email-exact results get the bonus
for r in email_results:  # CHANGED: don't include broad_email
    # ... existing logic ...
    if _is_linkedin_profile_url(r.get("url", "")):
        r["_email_evidence"] = True  # Only for email-exact now
        all_candidates.append(r)

# Process broad_email separately without the bonus
for r in broad_email:
    if _is_linkedin_profile_url(r.get("url", "")):
        # No _email_evidence flag — scored purely on name/slug/org signals
        all_candidates.append(r)
```

### Change 2: enrichers.py:205–215 + :330
```python
# Stronger weight for full-name match
if len(overlap) >= 2 and name_len >= 10:
    score += 3  # was +3 already for strong match; keep
elif len(overlap) >= 2:
    score += 3  # NEW: full first+last match always gets +3
else:
    score += 2

# Adjust acceptance threshold to require ≥1 positive non-name signal
# when there are ≥1 soft penalty
positive_non_name = 0  # count positive signals other than name
# ... existing checks tracking this ...
negative_signals = 0  # count soft penalties

if score >= 2 and (negative_signals == 0 or positive_non_name >= 1):
    return True, log
```

### Change 3: identity.py:502–514 (restore follow-evidence branch)
```python
def search(query: str, label: str):
    if query in queries_run:
        return [], []  # (li_results, non_li_results)
    queries_run.add(query)
    _search_count[0] += 1
    log.append(f"  Search ({label}): {query}")
    results = _web_search(query, self.brave_key, self.serper_key)
    li_results = [r for r in results if _is_linkedin_profile_url(r["url"])]
    non_li_results = [r for r in results if not _is_linkedin_profile_url(r["url"])]
    _record_evidence(non_li_results, label)
    log.append(f"    → {len(results)} results, {len(li_results)} LinkedIn profiles")
    return li_results, non_li_results

# Then at line 523:
email_li, email_non_li = search(f'"{email}"', "email-exact")
# At line 565, check the actual broker/org pages:
if email_non_li:
    log.append(f"  Following email evidence ({len(email_non_li)} pages)...")
    evidence_candidates = _follow_email_evidence(email_non_li, name, log)
    all_candidates.extend(evidence_candidates)
```

---

## 9. Cost/benefit summary

| Change | Effort | Cost to run experiment | Expected hit-rate lift (search-needed) | Risk |
|---|---|---|---|---|
| P1 | 1 line | $0 (offline replay) | +5–10% | low |
| P4 | 5 lines | $0 (offline replay) | overlaps P1 | low |
| P2 | ~15 lines | $0 (offline replay of stored data) | +5–11% | medium — may admit wrong people |
| P3 | ~15 lines | ~$0.50 (small live run) | +1–5% | low |
| P5 | ~20 lines | $0 | 0% (saves ~$2/1000) | low |
| **All combined** | ~1 day | ~$20 validation | **+11–25 points (56% → 67–81%)** | audit newly-accepted |

---

## 10. Case against (per analytical-discipline rule)

Strongest counterargument: **"The 56% hit rate is not the bottleneck — 8,529 pending profiles are."**

Response: True — but the pending queue will process under whatever the current pipeline logic is. Fixing the pipeline BEFORE running those 8,529 captures ~+1,800 profiles at current segment ratios if hit rate goes from 56% to 75%. Fixing after = those are lost (can't cheaply re-run without re-burning EnrichLayer credits, $143 at current cost per profile).

Counterfactual: **what happens if we do nothing?** Allan's 6,392 pending profiles run through the current pipeline. At current rates (56% search-needed, 73% preset-supplied, ~70% of Allan profiles are corp-email-with-org which is best segment) → expect ~3,500–4,000 enriched, ~$100–$150 EnrichLayer cost, ~1,500–2,000 profiles lost to the known failure modes diagnosed here.
