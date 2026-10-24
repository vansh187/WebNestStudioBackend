-- CodeLab API relay daily quotas (CODELAB_API_RELAY_BACKEND.md section 5).
--
-- codelab_relay_daily_usage: calls per learner per UTC day.
-- codelab_relay_global_daily_usage: calls by all learners per UTC day
-- (RELAY_GLOBAL_PER_DAY). A separate table rather than a user_id = 0 row,
-- because user ids are UUIDs with a foreign key to users.
--
-- Rows older than 30 days are deleted by a daily job. The app also creates
-- these tables at startup (create_all), so running this by hand is optional.
-- Safe to re-run.

create table if not exists codelab_relay_daily_usage (
    user_id    uuid not null references users(id) on delete cascade,
    day        date not null,
    count      integer not null default 0,
    updated_at timestamptz default now(),
    primary key (user_id, day)
);

create index if not exists ix_codelab_relay_daily_usage_day on codelab_relay_daily_usage (day);

create table if not exists codelab_relay_global_daily_usage (
    day        date primary key,
    count      integer not null default 0,
    updated_at timestamptz default now()
);
