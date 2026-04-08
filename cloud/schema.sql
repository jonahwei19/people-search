-- People Search Cloud — Database Schema
--
-- Run this in the Supabase SQL Editor to initialize the database.
-- Requires: pgcrypto extension (enabled by default in Supabase).
--
-- Tables:
--   accounts      — Organizations (name + password)
--   datasets      — Uploaded people datasets
--   profiles      — Individual people (one per row per dataset)
--   searches      — Saved search configurations
--   feedback      — User ratings on search results
--   global_rules  — Cross-search scoring rules
--   jobs          — Enrichment progress tracking
--
-- All tables use TEXT primary keys (compatible with Python's 8-char UUIDs).
-- Row-level security is enabled but bypassed by the service key; the Python
-- adapter filters by account_id in all queries.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── Accounts ────────────────────────────────────────────────

CREATE TABLE accounts (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  name TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  settings JSONB DEFAULT '{}'::jsonb   -- API keys, preferences
);

-- ── Datasets ────────────────────────────────────────────────

CREATE TABLE datasets (
  id TEXT PRIMARY KEY,                 -- from Python model (8-char UUID)
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  source_file TEXT DEFAULT '',
  total_rows INT DEFAULT 0,
  field_mappings JSONB DEFAULT '[]'::jsonb,
  searchable_fields JSONB DEFAULT '[]'::jsonb,
  enrichment_stats JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Profiles ────────────────────────────────────────────────

CREATE TABLE profiles (
  id TEXT PRIMARY KEY,                 -- from Python model (8-char UUID)
  dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,

  -- Identity
  name TEXT DEFAULT '',
  email TEXT DEFAULT '',
  linkedin_url TEXT DEFAULT '',
  organization TEXT DEFAULT '',
  title TEXT DEFAULT '',
  phone TEXT DEFAULT '',

  -- Links
  twitter_url TEXT DEFAULT '',
  website_url TEXT DEFAULT '',
  resume_url TEXT DEFAULT '',
  other_links JSONB DEFAULT '[]'::jsonb,

  -- Enriched data
  linkedin_enriched JSONB DEFAULT '{}'::jsonb,
  content_fields JSONB DEFAULT '{}'::jsonb,
  metadata JSONB DEFAULT '{}'::jsonb,
  fetched_content JSONB DEFAULT '{}'::jsonb,

  -- Profile card (compact text for LLM scoring)
  profile_card TEXT DEFAULT '',
  field_summaries JSONB DEFAULT '{}'::jsonb,

  -- Pipeline state
  enrichment_status TEXT DEFAULT 'pending',
  enrichment_log JSONB DEFAULT '[]'::jsonb,

  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_profiles_dataset ON profiles(dataset_id);
CREATE INDEX idx_profiles_account ON profiles(account_id);
CREATE INDEX idx_profiles_email ON profiles(email);
CREATE INDEX idx_profiles_linkedin ON profiles(linkedin_url);
CREATE INDEX idx_profiles_status ON profiles(enrichment_status);

-- ── Searches ────────────────────────────────────────────────

CREATE TABLE searches (
  id TEXT PRIMARY KEY,                 -- from Python model (8-char UUID)
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  clarification_context TEXT DEFAULT '',
  search_rules JSONB DEFAULT '[]'::jsonb,
  exemplars JSONB DEFAULT '[]'::jsonb,
  cache_prompt_hash TEXT DEFAULT '',
  cache_scores JSONB DEFAULT '{}'::jsonb,
  applicable_global_rule_ids JSONB DEFAULT '[]'::jsonb,
  excluded_profile_ids JSONB DEFAULT '[]'::jsonb,
  prompt_corrections JSONB DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Feedback ────────────────────────────────────────────────

CREATE TABLE feedback (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  profile_id TEXT DEFAULT '',
  profile_name TEXT DEFAULT '',
  rating TEXT DEFAULT '',
  reason TEXT,
  reasoning_correction TEXT,
  scope TEXT DEFAULT 'search',
  created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Global Rules ────────────────────────────────────────────

CREATE TABLE global_rules (
  id TEXT PRIMARY KEY,                 -- from Python model (8-char UUID)
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  scope TEXT DEFAULT 'all searches',
  source TEXT DEFAULT 'manual',
  created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Jobs ────────────────────────────────────────────────────

CREATE TABLE jobs (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
  status TEXT DEFAULT 'running',       -- running/embedding/done/error/cancelled
  current_count INT DEFAULT 0,
  total_count INT DEFAULT 0,
  message TEXT DEFAULT '',
  stats JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- ── Row-Level Security ──────────────────────────────────────
-- CRITICAL: RLS must be enabled on ALL tables, including accounts.
-- Without RLS, anyone with the anon key (public) can read/write all data.
-- The Python adapter uses the service key (bypasses RLS) and filters by
-- account_id in all queries.
--
-- If you add a new table, you MUST:
--   1. Add ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;
--   2. Add a policy (account_isolation or deny_anon)
--   3. Run both statements in the Supabase SQL Editor

ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE datasets ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE searches ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE global_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

-- Accounts: deny all direct access (login uses verify_login RPC)
CREATE POLICY deny_anon ON accounts FOR ALL USING (false);

CREATE POLICY account_isolation ON datasets
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

CREATE POLICY account_isolation ON profiles
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

CREATE POLICY account_isolation ON searches
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

CREATE POLICY account_isolation ON feedback
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

CREATE POLICY account_isolation ON global_rules
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

CREATE POLICY account_isolation ON jobs
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));

-- ── Helper Functions ────────────────────────────────────────

-- Profile count per dataset (used by list_datasets)
CREATE OR REPLACE FUNCTION dataset_profile_counts(p_account_id TEXT)
RETURNS TABLE(dataset_id TEXT, profile_count BIGINT)
LANGUAGE sql STABLE
AS $$
  SELECT p.dataset_id, COUNT(*)
  FROM profiles p
  WHERE p.account_id = p_account_id
  GROUP BY p.dataset_id;
$$;

-- Feedback count per search (used by list_searches)
CREATE OR REPLACE FUNCTION search_feedback_counts(p_account_id TEXT)
RETURNS TABLE(search_id TEXT, feedback_count BIGINT)
LANGUAGE sql STABLE
AS $$
  SELECT f.search_id, COUNT(*)
  FROM feedback f
  WHERE f.account_id = p_account_id
  GROUP BY f.search_id;
$$;

-- Create account with bcrypt password hash
-- Usage: SELECT create_account('BlueDot', 'secretpassword');
CREATE OR REPLACE FUNCTION create_account(p_name TEXT, p_password TEXT)
RETURNS TEXT
LANGUAGE sql
AS $$
  INSERT INTO accounts (name, password_hash)
  VALUES (p_name, crypt(p_password, gen_salt('bf')))
  RETURNING id;
$$;

-- Verify login credentials — returns row on success, empty on failure
-- Usage: SELECT * FROM verify_login('BlueDot', 'secretpassword');
CREATE OR REPLACE FUNCTION verify_login(p_name TEXT, p_password TEXT)
RETURNS TABLE(id TEXT, name TEXT, settings JSONB)
LANGUAGE sql STABLE
AS $$
  SELECT a.id, a.name, a.settings
  FROM accounts a
  WHERE a.name = p_name
    AND a.password_hash = crypt(p_password, a.password_hash);
$$;
