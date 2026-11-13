-- Node.js playground: the site-wide run counter, one row per UTC day, like
-- spring_playground_daily_usage. Kept in the database so a restart cannot reset the daily cap.
-- The project owner approves this before it's applied to Supabase. Apply it before deploying:
-- startup's create_all would otherwise build the table without row level security.
--
-- Row level security with no policies: the backend connects as the owner, which bypasses it,
-- and Supabase's anon and authenticated roles get nothing (see 019_close_public_table_access).
--
-- Safe to re-run. Until it is applied, Node.js runs fail closed with "unavailable"; nothing
-- else is affected.
--
-- Rollback:
--   drop table if exists node_playground_daily_usage;

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS node_playground_daily_usage (
    day date PRIMARY KEY,
    runs integer NOT NULL DEFAULT 0
);

ALTER TABLE node_playground_daily_usage ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON node_playground_daily_usage FROM anon, authenticated;

COMMIT;
