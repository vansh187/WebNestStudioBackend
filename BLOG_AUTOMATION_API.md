# Automated Blog System — Notes for the Frontend Team

**Status:** Backend live. Posts start publishing automatically — one today
(2026-07-26) at 6:00 PM IST, then every 2 days at 6:00 AM IST going forward.

## What changed

The blog isn't manually curated content anymore — the backend generates and
publishes a new SEO-optimized post on its own schedule, and each post
**auto-expires 15 days after its own publish date** (this includes the 2
original static posts too — nothing is permanent anymore, so don't build any
UI assumption that a post will always exist). An expired post simply stops
appearing in the list/detail endpoints; there's no "archived" state exposed
to the frontend.

The public endpoints you already use (`GET /api/blog`, `GET
/api/blog/{slug}`) are unchanged in shape and behavior except for **new
fields on every post**, added for SEO (meta tags, keyword targeting). Nothing
that previously worked should break — this is additive.

## Endpoints (public, no auth)

### `GET /api/blog?tag=<optional>`

Returns published, non-expired posts, newest first.

**Response — `200`**
```json
[
  {
    "id": "uuid",
    "title": "How Small Businesses Can Use AI Without a Dev Team",
    "slug": "how-small-businesses-can-use-ai-without-a-dev-team",
    "excerpt": "A short 1-2 line teaser for blog list cards.",
    "content": "## Heading\n\nMarkdown body, max ~300 words...",
    "cover_image_url": null,
    "author_id": null,
    "tags": ["ai for small business", "automation", "web development"],
    "is_published": true,
    "published_at": "2026-07-26T18:00:00+05:30",
    "expires_at": "2026-08-10T18:00:00+05:30",
    "meta_title": "AI for Small Business Without a Dev Team",
    "meta_description": "Practical ways small businesses can adopt AI tools today, no engineering team required. Get started with Webnest Studio.",
    "keywords": ["ai for small business", "small business automation", "no-code ai tools", "webnest studio ai solutions"],
    "topic_tag": "AI adoption for SMBs",
    "word_count": 287
  }
]
```

`?tag=` filters against the `tags` array (same as before — `keywords[:3]` is
what gets written into `tags`, so tag-filtering still works the same way).

### `GET /api/blog/{slug}`

Same shape as one item above. `404` if the post doesn't exist **or has
expired** — the backend doesn't distinguish "never existed" from "expired"
in the error, so don't build UI copy that assumes one or the other.

## New fields — what to do with them

| Field | Use for |
|---|---|
| `meta_title` | `<title>` tag / Open Graph title on the post page (falls back to `title` if you'd rather not use it, but `meta_title` is tuned to be ≤60 chars for search snippets) |
| `meta_description` | `<meta name="description">` and OG description — written to be 150–160 chars, don't truncate further |
| `keywords` | 4–6 SEO phrases; good for `<meta name="keywords">` and/or as on-page tag chips if you want them visible |
| `topic_tag` | Internal category label, distinct wording from the title — could work as a "Topic" badge on the post card, not meant to be keyword-stuffed |
| `word_count` | Just informational (e.g. "~287 words" / reading-time estimate) |
| `expires_at` | Not something to display to users — just be aware a post can vanish from the list between one page load and the next |

All of these can be `null` on the 2 original posts until the one-time DB
migration backfills them, and can theoretically be `null` on any
manually-created admin post — always code defensively (fall back to `title`
if `meta_title` is null, etc.), don't assume they're always populated.

## Sitemap / SEO

`GET /sitemap.xml` already includes every currently-published, non-expired
post automatically — nothing for the frontend to do there, just noting it so
you know new posts show up in the sitemap without a deploy.

## Behavior to design around

- **Posts disappear.** A post that was in the list yesterday may 404 today
  if it crossed its 15-day expiry. If you cache post lists client-side,
  don't cache slugs/links indefinitely.
- **New posts appear on a schedule, not on-demand.** Don't build a "check for
  new posts" polling UI expecting frequent updates — realistically once
  every ~2 days, occasionally more if an admin manually triggers one.
- **No "load more coming soon" placeholder needed** — the list endpoint
  always reflects exactly what's live right now.

## Admin-only endpoints (if you're building the admin panel)

All under `/api/admin`, require an admin bearer token (`Authorization:
Bearer <access_token>`), same auth pattern as the rest of `/api/admin/*`.

### `GET /api/admin/blog` — all posts (published, expired, drafts), for a management table

### `POST /api/admin/blog` / `PUT /api/admin/blog/{id}` / `DELETE /api/admin/blog/{id}`
Manual CRUD, unchanged from before except the request/response bodies now
also accept the SEO fields above. Note: if you `PUT` with `expires_at: null`
it does **not** make the post permanent — the backend resets it to the
standard 15-day window from `published_at` instead (there's no way to make a
post permanent through this API, by design).

### `POST /api/admin/blog/generate`
Manually fires the automated generation pipeline right now (bypasses the
2-day cooldown) — useful for a "Generate now" button if the admin panel
wants one. Always returns `200`, never a generation-related error status:
```json
{ "success": true, "detail": "Blog post generated and published", "post_id": "uuid", "slug": "..." }
```
or, if both LLM providers are temporarily down:
```json
{ "success": false, "detail": "All configured LLM providers failed to generate a response" }
```
Treat `success: false` as "try again shortly," not a bug report.

### `GET /api/admin/blog/generation-logs?limit=20`
Recent generation attempts (success/failure, which LLM was used, topic,
error message) — useful for an admin diagnostics view.

## Testing checklist

1. Hit `GET /api/blog` and confirm the new fields (`meta_title`,
   `meta_description`, `keywords`, `topic_tag`, `word_count`, `expires_at`)
   are present in the response (may be `null` until the DB migration runs).
2. Confirm `GET /api/blog/{slug}` for an existing slug still works.
3. Confirm `GET /api/blog/{slug}` for a made-up slug returns `404`.
4. After 2026-07-26 6:00 PM IST, confirm a new post shows up at the top of
   `GET /api/blog`.
5. If you render `meta_title`/`meta_description` in page `<head>` tags,
   verify they fall back gracefully to `title`/`excerpt` when null.
