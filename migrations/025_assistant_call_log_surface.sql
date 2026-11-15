-- Which app made each assistant call, so VS Code can be measured on its own.
-- Existing rows and Android/website calls stay 'codelab'.
--
-- Adding a column with a constant default only changes the table's description (no rewrite),
-- so it takes a brief lock; if something holds it, this gives up after 3 seconds; run it again.
-- The index is built CONCURRENTLY, outside the transaction, so writes to the call log (one per
-- AI call) are never blocked while it builds. Safe to re-run.
--
-- Apply BEFORE deploying the code. The project owner approves it before it's applied to Supabase.
--
-- Rollback:
--   drop index concurrently if exists ix_assistant_call_log_surface_created;
--   alter table assistant_call_log drop column if exists surface;

BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE assistant_call_log ADD COLUMN IF NOT EXISTS surface text NOT NULL DEFAULT 'codelab';
COMMIT;

-- Run on its own (not inside a transaction). If the build is interrupted it leaves an invalid
-- index: drop it with the rollback line above and run this again.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_assistant_call_log_surface_created
    ON assistant_call_log (surface, created_at);
