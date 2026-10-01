-- Audit trail for automated CodeLab problem generation: one row per
-- difficulty per run, success or failure. The scheduler reads it to decide
-- which difficulties are due, and a successful row's problem_slug identifies
-- an auto-generated problem.
--
-- The app also creates this table at startup (create_all), so running this
-- by hand is optional. Safe to re-run.

create table if not exists codelab_generation_logs (
    id             uuid primary key default gen_random_uuid(),
    attempted_at   timestamptz default now(),
    success        boolean not null,
    difficulty     text not null,
    topic          text,
    llm_used       text,
    problem_id     uuid references codelab_problems(id) on delete set null,
    problem_slug   text,
    error_message  text,
    trigger_source text not null
);

create index if not exists ix_codelab_generation_logs_attempted_at
    on codelab_generation_logs (attempted_at);
