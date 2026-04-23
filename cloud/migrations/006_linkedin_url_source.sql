-- Track where a profile's LinkedIn URL came from.
-- "user"     = uploaded by the user (ground truth — verifier trusts it)
-- "resolved" = found by identity search (strict verifier still applies)
-- ""         = not set
--
-- Paste at https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new:

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS linkedin_url_source TEXT DEFAULT '';

-- Back-fill: assume all existing LinkedIn URLs on v0-legacy rows came via
-- search (we can't distinguish retroactively). Rows with NO url stay ''.
-- This is conservative — it means the verifier keeps applying the strict
-- rules until those profiles are re-uploaded or re-enriched.
UPDATE profiles
SET linkedin_url_source = 'resolved'
WHERE linkedin_url <> '' AND (linkedin_url_source IS NULL OR linkedin_url_source = '');
