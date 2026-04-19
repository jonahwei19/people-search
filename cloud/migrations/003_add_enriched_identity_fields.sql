-- Add enriched_organization / enriched_title columns to profiles
-- Run in Supabase SQL editor:
--   https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new
--
-- Context: plans/diagnosis_correctness.md FM5.
-- Before this migration, `enrichers.py` backfilled `profile.organization` and
-- `profile.title` from whatever LinkedIn returned after a (sometimes wrong-
-- person) match was accepted. A wrong match therefore OVERWROTE the user's
-- uploaded ground truth, and on the next pipeline run the original
-- `organization` signal was gone — guaranteeing we couldn't re-evaluate with
-- the correct input.
--
-- Fix: never overwrite user-supplied org/title. The LinkedIn-sourced values
-- land here instead, leaving `profiles.organization` / `profiles.title` as
-- whatever the upload said. Downstream code can inspect `enriched_*` when it
-- wants the LinkedIn-reported value explicitly.
--
-- Idempotent — safe to re-run.

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS enriched_organization TEXT DEFAULT '';

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS enriched_title TEXT DEFAULT '';
