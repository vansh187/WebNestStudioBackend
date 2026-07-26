-- Run this once in the Supabase SQL editor before deploying the automated
-- blog generation feature. Safe to re-run (every statement is idempotent).

-- 1. New SEO / expiry columns on the existing blog_posts table.
--    Nullable so existing rows and any manually-created posts stay valid.
alter table blog_posts add column if not exists meta_title text;
alter table blog_posts add column if not exists meta_description text;
alter table blog_posts add column if not exists keywords text[];
alter table blog_posts add column if not exists topic_tag text;
alter table blog_posts add column if not exists word_count integer;
alter table blog_posts add column if not exists expires_at timestamptz;

create index if not exists idx_blog_posts_topic_tag on blog_posts (topic_tag);
create index if not exists idx_blog_posts_expires_at on blog_posts (expires_at);

-- 2. There are no "permanent" posts in this system - every post, including
--    the 2 existing static ones, expires 15 days after its own publish date.
--    Backfill expires_at for any row that doesn't have it yet (falls back to
--    created_at if published_at is somehow null).
update blog_posts
set expires_at = coalesce(published_at, created_at) + interval '15 days'
where expires_at is null;

-- 3. Audit trail for every automated generation attempt (success or
--    failure), so operators can see what happened without shell access to
--    application logs.
create table if not exists blog_generation_logs (
  id uuid primary key default gen_random_uuid(),
  attempted_at timestamptz not null default now(),
  success boolean not null,
  llm_used text,
  topic_tag text,
  blog_post_id uuid references blog_posts(id) on delete set null,
  error_message text,
  trigger_source text not null
);

create index if not exists idx_blog_generation_logs_attempted_at on blog_generation_logs (attempted_at desc);
