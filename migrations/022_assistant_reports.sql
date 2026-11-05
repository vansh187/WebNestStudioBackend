-- assistant_reports: members reporting an AI reply, as Google Play's AI-generated content
-- policy requires. The server keeps no chat history, so a report holds the text it is about.
--
-- A new, empty table, so creating it takes no lock that matters. Safe to re-run.
--
-- Rollback:
--   drop table if exists assistant_reports;

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS assistant_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    reason text NOT NULL,
    details text,
    reply_text text NOT NULL,
    prompt_text text,
    client_message_id text,
    provider_name text,
    model text,
    status text NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_by uuid REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT ck_assistant_report_reason CHECK (reason IN ('harmful', 'sexual', 'hateful', 'misleading', 'privacy', 'other')),
    CONSTRAINT ck_assistant_report_status CHECK (status IN ('open', 'resolved'))
);

CREATE INDEX IF NOT EXISTS ix_assistant_reports_status ON assistant_reports (status, created_at DESC);

ALTER TABLE assistant_reports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON assistant_reports FROM anon, authenticated;

COMMIT;
