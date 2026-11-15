-- WebNest AI for VS Code: device logins (PKCE), sessions and rotating refresh tokens.
-- One session per connected VS Code; users see and disconnect them on the website.
-- Login codes and refresh tokens are stored only as SHA-256 hashes, never in clear.
--
-- New, empty tables, so creating them takes no lock that matters. The foreign keys briefly
-- lock users: if something holds that lock, this gives up after 3 seconds; run it again.
--
-- Row level security with no policies: the backend connects as the owner, which bypasses it,
-- and Supabase's anon and authenticated roles get nothing (see 019_close_public_table_access).
--
-- Apply BEFORE deploying the code: startup's create_all would otherwise build these tables
-- without row level security. The project owner approves it before it's applied to Supabase.
-- Safe to re-run.
--
-- Rollback:
--   drop table if exists extension_refresh_tokens;
--   drop table if exists extension_auth_codes;
--   drop table if exists extension_sessions;

BEGIN;

SET LOCAL lock_timeout = '3s';

-- One row per connected VS Code (per login). id is the session_id the extension sees.
CREATE TABLE IF NOT EXISTS extension_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name text,
    app_name text,
    ext_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_reason text,
    CONSTRAINT ck_extension_session_revoked_reason
        CHECK (revoked_reason IS NULL OR revoked_reason IN ('logout', 'user', 'reuse', 'password_reset', 'admin'))
);

CREATE INDEX IF NOT EXISTS ix_extension_sessions_user ON extension_sessions (user_id) WHERE revoked_at IS NULL;

-- One-time login codes: single use, 5 minutes, bound to the PKCE challenge and redirect URI.
CREATE TABLE IF NOT EXISTS extension_auth_codes (
    code_hash text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_challenge text NOT NULL,
    redirect_uri text NOT NULL,
    device_name text,
    app_name text,
    ext_version text,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_extension_auth_codes_expires ON extension_auth_codes (expires_at);

-- Rotating refresh tokens. A refresh marks the old row used and inserts a new one.
CREATE TABLE IF NOT EXISTS extension_refresh_tokens (
    token_hash text PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES extension_sessions(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_extension_refresh_tokens_session ON extension_refresh_tokens (session_id);
CREATE INDEX IF NOT EXISTS ix_extension_refresh_tokens_expires ON extension_refresh_tokens (expires_at);

ALTER TABLE extension_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE extension_auth_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE extension_refresh_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON extension_sessions FROM anon, authenticated;
REVOKE ALL ON extension_auth_codes FROM anon, authenticated;
REVOKE ALL ON extension_refresh_tokens FROM anon, authenticated;

COMMIT;
