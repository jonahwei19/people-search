-- Add enrichment_version column to profiles
-- Run in Supabase SQL editor:
--   https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new
--
-- This column stamps which pipeline generation ("v1", "v2", …) produced a
-- given enriched record. Lets us re-enrich selectively when the pipeline
-- changes and track which profiles have stale data.

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS enrichment_version TEXT DEFAULT '';

-- Backfill: mark everything already enriched/skipped/failed as v1 since that
-- is the generation of pipeline that produced them.
UPDATE profiles
SET enrichment_version = 'v1'
WHERE enrichment_status IN ('enriched', 'skipped', 'failed')
  AND (enrichment_version IS NULL OR enrichment_version = '');
