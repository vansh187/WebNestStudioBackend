-- CodeLab AI coding assistant: calls and tokens per user, per UTC day.
-- Safe to re-run.
--
-- The foreign key briefly locks users. If something holds that lock, this gives up
-- after 3 seconds instead of making logins queue behind it: just run it again.
--
-- Indexes: the primary key serves the per-user lookups and the delete cascade, and
-- ix_assistant_usage_day serves the daily totals. Don't index the counter columns:
-- that stops Postgres updating them in place and slows every call.

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS assistant_usage (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day date NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, provider, model)
);

CREATE INDEX IF NOT EXISTS ix_assistant_usage_day ON assistant_usage(day);

COMMIT;
