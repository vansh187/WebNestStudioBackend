-- CodeLab AI coding assistant: one row per model call, for audit and cost tracking.
-- Numbers only, never message text or code. A deleted account's rows stay, with no
-- link to the person, so totals still add up. A daily job deletes rows older than
-- ASSISTANT_CALL_LOG_RETENTION_DAYS (default 90).
--
-- Safe to re-run. The foreign key briefly locks users: if something holds that lock,
-- this gives up after 3 seconds instead of making logins queue, so just run it again.
--
-- Row level security with no policies: the backend connects as the table's owner, which
-- bypasses it, and Supabase's anon and authenticated roles get nothing.
--
-- Rollback:
--   drop table if exists assistant_call_log;

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS assistant_call_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    provider text NOT NULL,
    model text NOT NULL,
    mode text NOT NULL,
    outcome text NOT NULL,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    cached_tokens bigint NOT NULL DEFAULT 0,
    exact_usage boolean NOT NULL DEFAULT false,
    request_chars integer NOT NULL DEFAULT 0,
    sent_chars integer NOT NULL DEFAULT 0,
    system_chars integer NOT NULL DEFAULT 0,
    tools_chars integer NOT NULL DEFAULT 0,
    file_chars integer NOT NULL DEFAULT 0,
    context_chars integer NOT NULL DEFAULT 0,
    user_chars integer NOT NULL DEFAULT 0,
    tool_result_chars integer NOT NULL DEFAULT 0,
    history_chars integer NOT NULL DEFAULT 0,
    first_output_ms integer,
    duration_ms integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_assistant_call_log_user_created ON assistant_call_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_assistant_call_log_created_at ON assistant_call_log(created_at);

ALTER TABLE assistant_call_log ENABLE ROW LEVEL SECURITY;

COMMIT;
