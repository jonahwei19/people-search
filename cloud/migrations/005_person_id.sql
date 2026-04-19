-- Phase 1 of app-wide profile merging.
-- Add a `person_id` that groups profile rows across datasets when they
-- represent the same person (same email, or same LinkedIn URL, or
-- same name+org).
--
-- Per-dataset rows still exist (they preserve the per-upload audit trail).
-- `person_id` is the key the UI and search use to deduplicate.
--
-- Paste this whole file in the Supabase SQL editor:
--   https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS person_id TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_profiles_person ON profiles(account_id, person_id);

-- Back-fill person_id by grouping existing rows.
-- Grouping key (in order of trust):
--   1. lower(email) if non-empty
--   2. lower(linkedin_url) if non-empty
--   3. lower(name) + '|' + lower(organization) as a weaker fallback
--
-- Rows that match on ANY key share a person_id. Rows with no matchable
-- identity get their own person_id (so they stay un-merged).
WITH keys AS (
  SELECT
    id,
    account_id,
    CASE
      WHEN email IS NOT NULL AND email <> '' THEN 'email:' || lower(email)
      WHEN linkedin_url IS NOT NULL AND linkedin_url <> '' THEN 'li:' || lower(linkedin_url)
      WHEN name IS NOT NULL AND name <> '' THEN 'name:' || lower(name) || '|' || coalesce(lower(organization), '')
      ELSE 'none:' || id
    END AS group_key
  FROM profiles
),
assigned AS (
  SELECT
    account_id,
    group_key,
    -- Deterministic person_id: hash of account+group_key, first 12 chars
    substring(md5(account_id || ':' || group_key), 1, 12) AS person_id
  FROM keys
  GROUP BY account_id, group_key
)
UPDATE profiles p
SET person_id = a.person_id
FROM keys k, assigned a
WHERE p.id = k.id
  AND k.account_id = a.account_id
  AND k.group_key = a.group_key
  AND (p.person_id IS NULL OR p.person_id = '');

-- Verify: count profiles vs persons per account.
-- SELECT account_id,
--        count(*) AS profile_rows,
--        count(DISTINCT person_id) AS unique_persons
-- FROM profiles
-- WHERE person_id <> ''
-- GROUP BY account_id;
