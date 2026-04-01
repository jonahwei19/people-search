Ask Claude how this works.

## Security

### Supabase Row-Level Security (RLS)

RLS **must** be enabled on every table. Without it, anyone with the project's anon key can read, edit, and delete all data.

- The Python backend uses the **service key**, which bypasses RLS. So RLS doesn't affect app functionality.
- RLS only blocks direct access via the public/anon key.

**When adding a new table**, always add to `cloud/schema.sql`:
```sql
ALTER TABLE new_table ENABLE ROW LEVEL SECURITY;
CREATE POLICY account_isolation ON new_table
  FOR ALL USING (account_id = current_setting('app.account_id', true))
  WITH CHECK (account_id = current_setting('app.account_id', true));
```

Then run both statements in the [Supabase SQL Editor](https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/sql/new).

**To verify:** Go to [Advisors > Security](https://supabase.com/dashboard/project/bbvwebtkypytvrhaeqey/advisors/security) — should show no critical issues.
