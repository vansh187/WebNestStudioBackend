-- User-generated content moderation: message reporting.
--
-- app.py's lifespan runs Base.metadata.create_all, so this table is created
-- automatically on deploy - this file is for review / explicit manual apply
-- against Supabase only. Nothing here ALTERs an existing table.
--
-- Blocking a user reuses the existing users.is_active flag (see
-- services/auth_service.py) - no schema change needed for that half of
-- moderation.
--
-- Rollback:
--   drop table if exists message_reports;

CREATE TABLE IF NOT EXISTS message_reports (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id        uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    reporter_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reported_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reason            text NOT NULL,
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_report_message_reporter UNIQUE (message_id, reporter_id)
);
CREATE INDEX IF NOT EXISTS ix_message_reports_status ON message_reports (status);
