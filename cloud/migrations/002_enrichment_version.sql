-- Add enrichment_version column to profiles
-- Run in Supabase SQL editor:
--   https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new
--
-- This column stamps which pipeline generation ("v1", "v2", …) produced a
-- given enriched record. Lets us re-enrich selectively when the pipeline
-- changes and track which profiles have stale data.
--
-- IMPORTANT — about the back-fill label:
-- Historical rows were NOT produced by v1 code. The `ENRICHMENT_VERSION`
-- constant in `enrichment/pipeline.py` was only introduced on 2026-04-19;
-- every profile enriched before that ran under legacy (un-versioned) code
-- whose exact shape we can no longer recover. We therefore stamp them
-- `v0-legacy` — an explicit "pre-versioning era" label that does NOT lie
-- about provenance. Rows stamped `v1` should only be ones that have
-- actually been run through pipeline.run_enrichment() since 2026-04-19.
--
-- To surface profiles that still need re-enrichment under current code:
--   SELECT id FROM profiles WHERE enrichment_version = 'v0-legacy';

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS enrichment_version TEXT DEFAULT '';

-- Back-fill: mark everything already enriched/skipped/failed as v0-legacy.
-- `pending` rows intentionally stay as empty string — they haven't been
-- touched by any pipeline yet, so they have no version to report.
UPDATE profiles
SET enrichment_version = 'v0-legacy'
WHERE enrichment_status IN ('enriched', 'skipped', 'failed')
  AND (enrichment_version IS NULL OR enrichment_version = '');
