-- Blog posts no longer auto-expire. Published posts stay published until
-- someone unpublishes them; expires_at remains only as an optional manual
-- field (default null).
--
-- Safe to re-run. Does NOT republish anything: posts that already expired
-- stay unpublished for the owner to review one by one.

-- 1. updated_at backs the sitemap <lastmod> and is now returned by
--    GET /api/blog and GET /api/blog/{slug}. The column normally already
--    exists; this is a guard for environments where it does not.
alter table blog_posts add column if not exists updated_at timestamptz default now();
update blog_posts
set updated_at = coalesce(published_at, created_at, now())
where updated_at is null;

-- 2. Posts already past their expiry that the daily sweep has not flipped
--    yet: unpublish them first, so step 3 cannot bring them back.
update blog_posts
set is_published = false
where is_published = true
  and expires_at is not null
  and expires_at <= now();

-- 3. Remove the automatic 15-day expiry from every live post.
update blog_posts
set expires_at = null
where is_published = true
  and expires_at is not null;
