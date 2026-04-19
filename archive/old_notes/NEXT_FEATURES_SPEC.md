# People Search — Next Features Spec

Three workstreams. Each is independent and can be worked on in parallel.

---

## Feature 1: LinkedIn Correction + Enrichment Improvements

### Problem
The LinkedIn pipeline has a high failure rate and sometimes matches the wrong person. Users currently have no way to flag wrong matches or manually provide the correct LinkedIn URL from within the search results.

### 1A: Wrong LinkedIn / Manual LinkedIn UI

**In the profile modal (when you click a name in search results or dataset view):**

1. Add a **"Wrong LinkedIn"** button next to the LinkedIn link. When clicked:
   - Clears the current `linkedin_url` and `linkedin_enriched` on the profile
   - Sets `enrichment_status` to `failed`
   - Adds to `enrichment_log`: "LinkedIn manually marked as wrong by user"
   - Shows a text input: "Paste the correct LinkedIn URL"
   - If user provides a URL → save it as `linkedin_url`, trigger re-enrichment for just that profile (call EnrichLayer, verify, build profile card)
   - Update in Supabase immediately (cloud) or dataset JSON (local)

2. Add a **"No LinkedIn"** button for profiles that don't have one. Shows the same text input for manual entry.

3. After correction, the profile card should be rebuilt and the search score recalculated on next search run.

**API endpoints needed:**
```
POST /api/profile/<id>/linkedin
Body: {"linkedin_url": "https://linkedin.com/in/..."} or {"linkedin_url": ""} to clear
```
This endpoint should:
- Update the profile's linkedin_url
- If URL provided: call EnrichLayer, verify match, update linkedin_enriched
- Rebuild profile_card
- Save to storage

### 1B: Enrichment Pipeline Improvements

The identity resolver (`enrichment/identity.py`) needs to be more aggressive about finding the right person and more careful about rejecting wrong matches.

**Improvements to implement:**

1. **Scrape the person's org website.** If the profile has an organization + org website, fetch the org's team/about page and look for the person's name + LinkedIn URL. Many org websites list team members with LinkedIn links. This is how Jonah manually found Nathan Leonard — the org's website linked to his LinkedIn.

   Implementation: In `_extract_context()`, extract the org's domain from the email. In the search steps, add a step that searches `site:{org_domain} "{name}"` and follows results to extract LinkedIn URLs.

2. **Use the profile's content to verify.** When we have rich content (pitches, bios, application essays), the verification step should compare the enriched LinkedIn profile's headline/experience against the content. If someone pitched "AI-enabled biological design tools" but the LinkedIn shows a boilermaker, that's a mismatch even if the name matches.

   Implementation: In `_verify_match()`, add a content-relevance check. Extract key terms from `content_fields`, check if any appear in the enriched profile's `context_block`. If zero overlap on a profile with 200+ chars of content, add a penalty.

3. **Try the person's actual name variations.** Some people go by different names on LinkedIn vs. their formal name. Try searching with just first name + org, or just last name + org, not only "First Last".

4. **Better slug matching.** LinkedIn slugs like `nathan-l-318873b7` contain partial names. The current slug scorer should handle initial-based slugs: if the slug starts with `{first_initial}{last}`, that's a match.

5. **Log WHY each candidate was rejected** during verification, not just "wrong person." Include what mismatched (org, location, content) so users can see the reasoning.

### Verification
- [ ] Can click "Wrong LinkedIn" on a profile in search results
- [ ] Can paste a correct LinkedIn URL and it enriches immediately
- [ ] Can click "No LinkedIn" and manually add one
- [ ] Corrections persist (saved to Supabase/JSON)
- [ ] Profile card is rebuilt after correction
- [ ] Org website scraping finds LinkedIn URLs on team pages
- [ ] Content-relevance check rejects "boilermaker" for a biosecurity pitcher
- [ ] Enrichment log shows detailed rejection reasons

---

## Feature 2: Search Scoring + Synthesis Improvements

### Problem
Three issues with search scoring:
1. **Score clustering at 95** — Gemini 3.1 Flash-Lite gives too many profiles the same high score, making it hard to differentiate.
2. **Synthesis rules are too generic** — the LLM-generated rules from feedback are vague and don't capture what the user actually means.
3. **Feedback without reasons is ignored** — double-X'ing someone without typing a reason has no effect on future searches.

### 2A: Score Differentiation

**Option 1: Re-rank the top tier with a stronger model.**
After the initial Gemini Flash-Lite scoring, take all profiles scoring 90+ and re-score them with a stronger model (Gemini Pro or Claude Sonnet) that's instructed to spread scores across the 90-100 range. This is cheap (only ~10-20 profiles) and gives meaningful differentiation at the top.

Implementation:
- After `score_profiles_sync()` returns, filter profiles with score >= 90
- Re-score just those with a prompt that says: "These profiles all scored 90+. Rank them relative to each other on a 90-100 scale. Use the FULL range — only 1-2 profiles should score 98+."
- Replace their scores with the re-ranked versions

**Option 2: Better prompting for the initial scorer.**
The current prompt says "90-100 = exceptional match." Add calibration: "Only 5% of profiles should score 90+. Most profiles should score 30-70. If you're giving more than 10% of profiles a 90+, you're being too generous."

**Recommendation: Do both.** Option 2 first (free), Option 1 if clustering persists.

### 2B: Better Synthesis

The current synthesis prompt (`search/feedback.py`) produces generic rules. Improve it:

1. **Include the actual profile data** in the synthesis prompt. Don't just say "User rejected Profile X" — include the profile card so the LLM can see WHY the user rejected it.

2. **Pattern detection across feedback.** If the user rejects 3 profiles that are all academics with no shipping experience, the synthesis should produce a rule like "Reject academics without shipping experience" — not three separate rules about each person.

3. **Negative exemplars.** Currently exemplars are only positive. When a user double-X's someone, that profile should become a negative exemplar (score 5-10) that calibrates the scorer on what "bad" looks like.

4. **Rule deduplication.** The current system accumulates redundant rules. After synthesis, deduplicate by having the LLM consolidate overlapping rules.

### 2C: Feedback Without Reasons

When a user clicks ✗✗ without providing a reason:

1. **Infer the reason from the profile.** Send the profile to the LLM with the prompt: "The user rejected this profile for search '{query}'. Based on the profile, what is the most likely reason? Reply in one sentence." Store this as an auto-generated reason.

2. **Always create a negative exemplar.** Even without a reason, add the profile as a negative exemplar (score 5) with the auto-generated reason. This ensures double-X has an effect on future scoring.

3. **UI: show a subtle prompt.** After clicking ✗✗, briefly highlight the reason input field with "Why? (helps future searches)" — don't block, just nudge.

### 2D: Remove/Exclude from Search

Add the ability to remove a profile from search results without providing negative feedback. Use cases:
- Already being considered / in pipeline
- Accepted another job
- Already works at your company
- Any idiosyncratic reason that isn't about profile quality

**UI:**
- Add a **circle-X** button (○) on each search result, separate from the rating buttons
- When clicked, the profile is hidden from this search's results
- Hover tooltip: "Remove from results (not negative feedback — just hiding)"
- Excluded profile IDs stored on the search object: `excluded_profile_ids: list[str]`
- Results endpoint filters them out
- Can be undone: "Show N hidden profiles" link at bottom of results

**Data model change:**
```python
class DefinedSearch:
    ...
    excluded_profile_ids: list[str] = []  # profiles hidden from results
```

**API:**
```
POST /api/search/searches/<id>/exclude
Body: {"profile_id": "...", "reason": "already in pipeline"}  # reason optional

POST /api/search/searches/<id>/unexclude
Body: {"profile_id": "..."}
```

### Verification
- [ ] Top-tier re-ranking produces differentiated scores in the 90-100 range
- [ ] Calibration prompt reduces the number of 90+ scores
- [ ] Synthesis produces specific, actionable rules (not "exclude bad profiles")
- [ ] Double-X without reason auto-generates a reason and creates a negative exemplar
- [ ] Negative exemplars appear in the judge prompt and affect scoring
- [ ] Can exclude a profile from search results
- [ ] Excluded profiles don't appear in results
- [ ] Can un-exclude and see them again
- [ ] Hover tooltip explains what exclude means

---

## Feature 3: Prompt Engineering Automation

### Problem
The user's feedback should automatically improve future search quality, but the current feedback → rules → scoring loop is lossy. The synthesis is too generic, the rules accumulate without being tested, and there's no way to know if changes actually improved results.

### 3A: Feedback-to-Prompt Translation

Design a system that translates user feedback into concrete prompt changes, not just text rules.

**Types of feedback and how they should affect the prompt:**

| Feedback | Current effect | Should become |
|----------|---------------|---------------|
| ✗✗ with reason "too academic" | Text rule added | Negative exemplar + rule + system prompt calibration |
| ✗✗ without reason | Nothing | Auto-inferred reason + negative exemplar |
| ★ on a profile | Positive exemplar | Positive exemplar + extract what makes it good |
| Edit judge reasoning | Stored but unused | Direct prompt correction ("When you see X, say Y not Z") |
| Multiple ✗✗ on similar profiles | Multiple redundant rules | Pattern detection → single consolidated rule |

**Implementation:**

1. **After each feedback event**, immediately classify it:
   - Is this about the PROFILE (wrong person for this search)?
   - Is this about the SCORING (judge's reasoning was wrong)?
   - Is this GLOBAL (applies to all searches)?

2. **For profile-level feedback:** Add as exemplar. Extract the distinguishing features. "This person scored 95 but should have scored 20 because they're an academic with no shipping experience. Key signal: title contains 'Professor', no startup/product experience in work history."

3. **For scoring-level feedback:** Create a prompt correction. If the user edited the reasoning from "Strong technical builder" to "Academic researcher, no evidence of shipping", that becomes: "CORRECTION: When a profile lists only academic positions (Professor, Researcher, Postdoc), do not describe them as a 'builder' unless they have explicit startup, product, or open-source shipping experience."

4. **For global feedback:** Propose a scoped rule ("When evaluating seniority, ...") and add it to global rules after user confirms.

### 3B: A/B Testing Framework (optional, future)

Store scoring results with their prompt version hash. When rules change, compare new scores against old scores on the same profiles. Surface: "After your feedback, 12 profiles changed score by >10 points. Want to review?"

This is complex — spec it but don't build it in this iteration. Just store the prompt hash and scores so we can compare later.

### Verification
- [ ] Feedback without reason auto-generates a classification
- [ ] Negative exemplars include extracted key signals
- [ ] Reasoning corrections become prompt corrections in the system prompt
- [ ] Pattern detection consolidates similar feedback into one rule
- [ ] Rules are deduplicated after synthesis
- [ ] Prompt hash is stored with each scoring run for future comparison

---

## Implementation Notes

### Shared code locations
- Profile modal UI: `cloud/public/index.html` (search for `showProfile` function) and `local/upload_web.py`
- Enrichment pipeline: `enrichment/identity.py`, `enrichment/enrichers.py`
- Search scoring: `search/llm_judge.py`
- Feedback synthesis: `search/feedback.py`
- Search API routes: `api/search/` (cloud) and `local/search_blueprint.py` (local)
- Search models: `search/models.py` (DefinedSearch, FeedbackEvent, etc.)

### Both local and cloud
All features should work in both deployment modes. UI changes go in both `cloud/public/index.html` and `local/upload_web.py` (or extract the shared HTML into a single file). API changes go in both `api/` and `local/search_blueprint.py`.

### Testing
- Test enrichment improvements against the EAG dataset (25 profiles) — check if failure rate drops
- Test scoring improvements by re-running the "Young founder" search and comparing score distribution before/after
- Test UI features in the local app first (faster iteration), then verify in cloud
