CREATE TABLE IF NOT EXISTS coding_projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title text NOT NULL,
    description text NOT NULL DEFAULT '',
    language text NOT NULL,
    files json NOT NULL,
    stdin text NOT NULL DEFAULT '',
    is_public boolean NOT NULL DEFAULT false,
    share_id text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_coding_projects_owner_user_id ON coding_projects(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_coding_projects_language ON coding_projects(language);
CREATE INDEX IF NOT EXISTS ix_coding_projects_created_at ON coding_projects(created_at);
CREATE INDEX IF NOT EXISTS ix_coding_projects_updated_at ON coding_projects(updated_at);

CREATE TABLE IF NOT EXISTS coding_shares (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    share_id text NOT NULL UNIQUE,
    project_id uuid REFERENCES coding_projects(id) ON DELETE SET NULL,
    owner_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    title text NOT NULL,
    language text NOT NULL,
    files json NOT NULL,
    stdin text NOT NULL DEFAULT '',
    stdout text NOT NULL DEFAULT '',
    author_display_name text,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_coding_shares_share_id ON coding_shares(share_id);
CREATE INDEX IF NOT EXISTS ix_coding_shares_owner_user_id ON coding_shares(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_coding_shares_created_at ON coding_shares(created_at);

CREATE TABLE IF NOT EXISTS coding_executions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    ip_hash text,
    language text NOT NULL,
    status text NOT NULL,
    time_ms integer,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_coding_executions_user_id ON coding_executions(user_id);
CREATE INDEX IF NOT EXISTS ix_coding_executions_ip_hash ON coding_executions(ip_hash);
CREATE INDEX IF NOT EXISTS ix_coding_executions_language ON coding_executions(language);
CREATE INDEX IF NOT EXISTS ix_coding_executions_status ON coding_executions(status);
CREATE INDEX IF NOT EXISTS ix_coding_executions_created_at ON coding_executions(created_at);
