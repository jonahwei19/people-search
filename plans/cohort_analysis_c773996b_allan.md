# Cohort Analysis — Allan dataset `c773996b` (Invite tracker — TLS book launch — IFP Core 250)

Generated 2026-04-19 via `python -m enrichment.eval.cohort_analysis --local datasets/c773996b.json`.

## TL;DR

- Total profiles: **302**. Enriched **154 (51%)**, skipped **92 (30%)**, failed **56 (19%)**. This is ~9 pts lower than the pre-verifier-change baseline (60.9%) in plans/baseline_coverage_report.txt — the FM2/P2 strengthening correctly rejected a wave of strong-name + org-mismatch cases that were wrong-person.
- **Biggest improvement target: the `org_present` cohort's 21% failed rate.** 249 profiles have an org, 53 of those failed the verifier. 28 of the failures are `weak_match_hard_reject` (short single-token name overlap) and 29 are `org_mismatch`. Both are addressable — the slug-aware verifier work in flight will catch many of these, and P3 (dead `_follow_email_evidence` branch) would convert a subset directly.
- **Biggest absolute opportunity: the 37-profile `name+corp-email+no-org` bucket** (5.4% enriched). 16 of 37 hit `ambiguous_tie`. The email-username +20 fix already shipped but the slug-aware tie-breaker hasn't — this bucket is where P1+P4 land hardest.
- **Unsalvageable without new input: the 16-profile `name+personal-email+no-org` bucket** (0% enriched). Name + Gmail with nothing else is the canonical worst case; `no_linkedin_found` and `ambiguous_tie` own the failures. Do not spend API budget here until upstream intake adds org or title.
- Overall wrong-person rate on the enriched 154 is **1.3%** (lower bound). Down from 23% in the pre-FM2 baseline — the verifier changes are working as intended.
- Cost: **$12.38 for 302 profiles ($0.041/profile)**. The `corp×has-org` bucket is most expensive ($0.070/profile, 8.3 search queries average) — the verify-retry loop is firing more there. Budget for the remaining 6,392 Allan profiles at current cost: **~$260**.

## Recommended ordering (by projected ΔN per $ spent)

1. **Ship the slug-aware tie-breaker for `no-org` cohorts** (P4). 21 profiles in `no_org` hit `ambiguous_tie`; a tie-breaker that promotes candidates with both first+last in the slug would recover an estimated 10–15 of those. ~$0 to validate via replay.
2. **Follow email-exact non-LinkedIn evidence on edu+gov profiles** (P3). 8 of the 11 edu failures are either `no_linkedin_found` or `enrichlayer_error` despite the person being listed on their faculty page. Re-enabling `_follow_email_evidence` for .edu/.gov should convert ~5 profiles at ~$0.25 experimental cost.
3. **Stop processing `name+personal-email+no-org`** (guardrail). Zero hits. Put this cohort behind a flag that requires manual review before spending API budget.
4. **Expect the slug-aware+arbiter in-flight work to land the bulk of the 28 `weak_match_hard_reject`s.** These are strong-signal profiles where the verifier's conservative short-token guard fires; an arbiter that looks at the URL slug will overturn most of them.

## Snapshot

- Total profiles: **302**
- Overall enriched: **154 (51.0%)**
- Overall wrong-person rate (in enriched): **1.3%** (2 flagged)
- Total cost (estimated from logs): **$12.38** (1019 searches + 373 LinkedIn calls)

## By `email_type`

| bucket | N | enriched% | skipped% | failed% | wrong-person% | $/profile | top failure reasons |
|---|---|---|---|---|---|---|---|
| `org` | 136 | 44.1% | 30.9% | 25.0% | 0.0% | $0.047 | enrichlayer_error(34), ambiguous_tie(29), weak_match_hard_reject(25), org_mismatch(19), api_no_data(9) |
| `personal` | 101 | 61.4% | 29.7% | 8.9% | 3.2% | $0.030 | no_linkedin_found(17), enrichlayer_error(9), ambiguous_tie(9), org_mismatch(3), weak_match_hard_reject(2) |
| `corp` | 34 | 52.9% | 26.5% | 20.6% | 0.0% | $0.063 | enrichlayer_error(7), ambiguous_tie(5), name_mismatch(4), org_mismatch(4), api_no_data(3) |
| `gov` | 20 | 55.0% | 30.0% | 15.0% | 0.0% | $0.025 | no_linkedin_found(3), enrichlayer_error(3), org_mismatch(2), ambiguous_tie(2), api_no_data(1) |
| `edu` | 11 | 27.3% | 45.5% | 27.3% | 0.0% | $0.024 | no_linkedin_found(5), enrichlayer_error(3), name_mismatch(2), org_mismatch(1) |

## By `org_presence`

| bucket | N | enriched% | skipped% | failed% | wrong-person% | $/profile | top failure reasons |
|---|---|---|---|---|---|---|---|
| `org_present` | 249 | 61.0% | 17.7% | 21.3% | 1.3% | $0.046 | enrichlayer_error(53), org_mismatch(29), weak_match_hard_reject(28), ambiguous_tie(24), no_linkedin_found(18) |
| `no_org` | 53 | 3.8% | 90.6% | 5.7% | 0.0% | $0.018 | ambiguous_tie(21), no_linkedin_found(15), enrichlayer_error(3), name_mismatch(3), score_below_threshold(3) |

## By `name_length`

| bucket | N | enriched% | skipped% | failed% | wrong-person% | $/profile | top failure reasons |
|---|---|---|---|---|---|---|---|
| `two_token` | 275 | 53.5% | 26.9% | 19.6% | 1.4% | $0.043 | enrichlayer_error(54), ambiguous_tie(43), weak_match_hard_reject(28), org_mismatch(27), no_linkedin_found(26) |
| `three_plus_token` | 11 | 36.4% | 45.5% | 18.2% | 0.0% | $0.026 | no_linkedin_found(4), enrichlayer_error(2), org_mismatch(2), ambiguous_tie(1) |
| `no_name` | 9 | 0.0% | 100.0% | 0.0% | 0.0% | $0.000 | — |
| `single_token` | 7 | 42.9% | 57.1% | 0.0% | 0.0% | $0.027 | no_linkedin_found(3), ambiguous_tie(1) |

## By `email_x_org`

| bucket | N | enriched% | skipped% | failed% | wrong-person% | $/profile | top failure reasons |
|---|---|---|---|---|---|---|---|
| `org×has-org` | 108 | 53.7% | 15.7% | 30.6% | 0.0% | $0.055 | enrichlayer_error(33), weak_match_hard_reject(25), org_mismatch(19), ambiguous_tie(15), api_no_data(9) |
| `personal×has-org` | 85 | 72.9% | 16.5% | 10.6% | 3.2% | $0.033 | enrichlayer_error(9), no_linkedin_found(9), ambiguous_tie(4), org_mismatch(3), weak_match_hard_reject(2) |
| `corp×has-org` | 29 | 62.1% | 17.2% | 20.7% | 0.0% | $0.070 | enrichlayer_error(6), org_mismatch(4), api_no_data(3), name_mismatch(3), ambiguous_tie(3) |
| `org×no-org` | 28 | 7.1% | 89.3% | 3.6% | 0.0% | $0.015 | ambiguous_tie(14), no_linkedin_found(4), score_below_threshold(2), enrichlayer_error(1), name_mismatch(1) |
| `gov×has-org` | 17 | 64.7% | 17.6% | 17.6% | 0.0% | $0.024 | enrichlayer_error(3), org_mismatch(2), ambiguous_tie(2), no_linkedin_found(1), api_no_data(1) |
| `personal×no-org` | 16 | 0.0% | 100.0% | 0.0% | 0.0% | $0.019 | no_linkedin_found(8), ambiguous_tie(5) |
| `edu×has-org` | 10 | 30.0% | 50.0% | 20.0% | 0.0% | $0.024 | no_linkedin_found(5), enrichlayer_error(2), name_mismatch(1), org_mismatch(1) |
| `corp×no-org` | 5 | 0.0% | 80.0% | 20.0% | 0.0% | $0.021 | ambiguous_tie(2), no_linkedin_found(1), enrichlayer_error(1), name_mismatch(1), score_below_threshold(1) |
| `gov×no-org` | 3 | 0.0% | 100.0% | 0.0% | 0.0% | $0.028 | no_linkedin_found(2) |
| `edu×no-org` | 1 | 0.0% | 0.0% | 100.0% | 0.0% | $0.029 | enrichlayer_error(1), name_mismatch(1) |

## By `input_quality`

| bucket | N | enriched% | skipped% | failed% | wrong-person% | $/profile | top failure reasons |
|---|---|---|---|---|---|---|---|
| `name+corp-email+org` | 164 | 54.9% | 18.3% | 26.8% | 0.0% | $0.053 | enrichlayer_error(44), org_mismatch(26), weak_match_hard_reject(26), ambiguous_tie(20), api_no_data(13) |
| `name+personal-email+org` | 85 | 72.9% | 16.5% | 10.6% | 3.2% | $0.033 | enrichlayer_error(9), no_linkedin_found(9), ambiguous_tie(4), org_mismatch(3), weak_match_hard_reject(2) |
| `name+corp-email+no-org` | 37 | 5.4% | 86.5% | 8.1% | 0.0% | $0.018 | ambiguous_tie(16), no_linkedin_found(7), enrichlayer_error(3), name_mismatch(3), score_below_threshold(3) |
| `name+personal-email+no-org` | 16 | 0.0% | 100.0% | 0.0% | 0.0% | $0.019 | no_linkedin_found(8), ambiguous_tie(5) |

## Recommendations

- **corp** (N=34, enriched=52.9%, cost/profile=$0.063): corp-email cohort is best-case. Make sure the email-verified-org bonus fires — it should dominate scoring. If wrong-person rate is elevated, name-match strengthening (P2) helps more than scoring weights.
- **gov** (N=20, enriched=55.0%, cost/profile=$0.025): gov cohort performs reasonably — org-site crawl pays off here because agency pages are public. Consider running this cohort with a dedicated gov-site crawler first.
- **edu** (N=11, enriched=27.3%, cost/profile=$0.024): enriched rate is low. Academic faculty pages and .edu team listings often contain the LinkedIn URL in HTML but aren't being followed. Consider an `org-website` crawler or an OpenAlex lookup step before LinkedIn search. P3 dead-code fix is especially relevant here.
- **Hardest input-quality cohort: `name+personal-email+no-org`** — N=16, enriched=0.0%. This is the segment to target with pipeline improvements; use it as the denominator for any A/B experiment guardrails.
- **no-org cohort** — N=53, enriched=3.8%. Org is the single strongest verification signal; without it the scoring leans entirely on name + slug. Consider pre-inferring org from email domain earlier, or surfacing this cohort for manual review instead of auto-enriching.

## Method notes

- Metrics computed from stored `enrichment_log`, not live API. Cost is an estimate (see `enrichment/eval/coverage_report.py`).
- Wrong-person rate is a lower bound from `wrong_person_audit.audit_profile` (token-overlap + last-name heuristic; see that module for caveats).
- Failure reasons are the set of pattern-matches on log lines, one-hot per profile. A profile with multiple failure modes counts once per bucket.
