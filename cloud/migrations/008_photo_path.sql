-- Cached profile photo path (relative to facebook-photos bucket).
-- Empty string means "not yet cached".
--
-- Paste at https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new:

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS photo_path TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_profiles_photo_path ON profiles(photo_path) WHERE photo_path <> '';

-- Bucket creation is handled separately via the storage API (see
-- enrichment/photos.ensure_bucket()).  We just declare the column here.
