-- FCM registration tokens for push notifications (chat messages, new blog
-- posts). One row per installed app; a token moves to whoever last signed in
-- on that device.
--
-- The app also creates this table at startup (create_all), so running this
-- by hand is optional. Safe to re-run.

create table if not exists device_tokens (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references users(id) on delete cascade,
    token        text not null unique,
    platform     text not null default 'android',
    created_at   timestamptz default now(),
    last_seen_at timestamptz default now()
);

create index if not exists ix_device_tokens_user_id on device_tokens (user_id);
