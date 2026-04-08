-- Add missing columns to searches table
-- Run in Supabase SQL Editor: https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new

ALTER TABLE searches ADD COLUMN IF NOT EXISTS excluded_profile_ids JSONB DEFAULT '[]'::jsonb;
ALTER TABLE searches ADD COLUMN IF NOT EXISTS prompt_corrections JSONB DEFAULT '[]'::jsonb;
