-- assistant_call_log: the current turn's assistant text and tool-call arguments get their
-- own column, so history_chars only ever counts earlier turns.
--
-- Safe to re-run. Adding a column with a constant default doesn't rewrite the table. If
-- something holds a lock on it, this gives up after 3 seconds, so just run it again.
--
-- Rollback:
--   alter table assistant_call_log drop column if exists turn_assistant_chars;

BEGIN;

SET LOCAL lock_timeout = '3s';

ALTER TABLE assistant_call_log ADD COLUMN IF NOT EXISTS turn_assistant_chars integer NOT NULL DEFAULT 0;

COMMIT;
