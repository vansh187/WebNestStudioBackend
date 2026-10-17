-- CodeLab GitHub sync (GITHUB_INTEGRATION_BACKEND.md).
--
-- github_connections: one linked GitHub account per WebNest user. The OAuth
-- token is Fernet-encrypted by the app; this table never holds it in clear.
-- github_oauth_states: single-use OAuth `state` values (SHA-256 only), bound
-- to the user who started the flow, valid for 10 minutes.
--
-- The app also creates these tables at startup (create_all), so running this
-- by hand is optional. Safe to re-run.

create table if not exists github_connections (
    id                     uuid primary key default gen_random_uuid(),
    user_id                uuid not null unique references users(id) on delete cascade,
    github_user_id         bigint not null,
    login                  text not null,
    avatar_url             text,
    scopes                 text[] not null default '{}',
    access_token_encrypted text not null,
    connected_at           timestamptz default now(),
    updated_at             timestamptz default now()
);

create index if not exists ix_github_connections_user_id on github_connections (user_id);

create table if not exists github_oauth_states (
    state_hash text primary key,
    user_id    uuid not null references users(id) on delete cascade,
    return_to  text not null,
    created_at timestamptz default now(),
    expires_at timestamptz not null,
    used_at    timestamptz
);

create index if not exists ix_github_oauth_states_user_id on github_oauth_states (user_id);
create index if not exists ix_github_oauth_states_expires_at on github_oauth_states (expires_at);
