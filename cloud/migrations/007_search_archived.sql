-- Soft-archive flag for searches. Archived searches are hidden from the
-- default list but kept in the database (and unarchive-able).
--
-- Paste at https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new:

ALTER TABLE searches
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_searches_archived_at ON searches(archived_at);
