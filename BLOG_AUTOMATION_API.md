# Automated Blog System — Notes for the Frontend Team

**Status:** Backend live. One new post a week (06:00 IST), plus any an admin
triggers manually.

## What changed (October 2026)

- **Posts are permanent.** The 15-day auto-expiry is gone: a published post
  stays published until someone unpublishes it. `expires_at` is now an
  optional manual field and is `null` by default.
- **`GET /api/blog/{slug}` is `404` for unpublished or expired posts**, so it
  always agrees with the list.
- **`updated_at` is returned on every post** (both endpoints) - use it for
  `<lastmod>` / `dateModified`.
- **No duplicate posts.** If the generator's draft matches an existing post
  (same `topic_tag`, a closely matching title, the same primary keyword on an
  overlapping title, or a copied body) it first asks the model for a different
  topic; if the draft still matches a **published** post, that post is updated
  in place - same slug, new `updated_at` - instead of a second post being
  created. Two cases publish nothing and are logged as a failed run (retried
  at the next daily check): the draft is shorter than the post it matches, or
  the post it matches is unpublished (the generator never republishes a post;
  that is an admin decision). Slugs never get a
  numeric suffix such as `-2`, and the slug of a post that has been published
  cannot be changed through the admin API.
- **`meta_title` and `meta_description` are unique** across published posts.
- **Longer posts.** The generator targets 800-1,200 words: intro, 3-5 `##`
  sections, a short FAQ (`###` questions), one call to action.
- **The frontend is rebuilt automatically.** After a post is published,
  updated, unpublished or deleted the backend calls the Vercel deploy hook
  (`VERCEL_DEPLOY_HOOK_URL`). A failed hook call is logged and never fails
  the publish.

The public endpoints are unchanged in shape - fields were only added.

## Editorial direction

When the generator is not given a manual `topic_hint`, it now prioritizes
commercial-intent topic clusters tied to Webnest's core services:

- **Website Development** -> website development cost in India, React frontend
  with Python/Java backend development, conversion, ecommerce cost, and the
  Website Development service page.
- **AI & Automation** -> AI chatbot cost, lead automation, WhatsApp automation,
  AI search/GEO, and the AI Development service page.
- **CRM & Enterprise Software** -> custom CRM cost, Custom CRM vs Zoho vs
  Salesforce, SaaS vs custom software, and the CRM Development service page.

Manual admin generation can still force an exact subject through `topic_hint`;
otherwise the automated schedule should favor these clusters while avoiding
recently covered topics and near-duplicate titles.

## Endpoints (public, no auth)

### `GET /api/blog?tag=<optional>`

Returns **every** published post, newest first. Not paginated - a plain JSON
array, so the build can pre-render one page per item.

**Response — `200`**
```json
[
  {
    "id": "uuid",
    "title": "How Small Businesses Can Use AI Without a Dev Team",
    "slug": "how-small-businesses-can-use-ai-without-a-dev-team",
    "excerpt": "A short 1-2 line teaser for blog list cards.",
    "content": "Intro...\n\n## Heading\n\nMarkdown body, 800-1,200 words...",
    "cover_image_url": null,
    "author_id": null,
    "tags": ["ai for small business", "automation", "web development"],
    "is_published": true,
    "published_at": "2026-07-26T18:00:00+05:30",
    "expires_at": null,
    "updated_at": "2026-07-26T18:00:00+05:30",
    "meta_title": "AI for Small Business Without a Dev Team",
    "meta_description": "Practical ways small businesses can adopt AI tools today, no engineering team required. Get started with Webnest Studio.",
    "keywords": ["ai for small business", "small business automation", "no-code ai tools", "webnest studio ai solutions"],
    "topic_tag": "AI adoption for SMBs",
    "word_count": 960
  }
]
```

`?tag=` filters against the `tags` array (same as before — `keywords[:3]` is
what gets written into `tags`, so tag-filtering still works the same way).

### `GET /api/blog/{slug}`

Same shape as one item above. `404` if the post doesn't exist, is
unpublished, or is past a manually-set `expires_at`.

## New fields — what to do with them

| Field | Use for |
|---|---|
| `meta_title` | `<title>` tag / Open Graph title on the post page (falls back to `title` if you'd rather not use it, but `meta_title` is tuned to be ≤60 chars for search snippets) |
| `meta_description` | `<meta name="description">` and OG description — written to be 150–160 chars, don't truncate further |
| `keywords` | 4–6 SEO phrases; good for `<meta name="keywords">` and/or as on-page tag chips if you want them visible |
| `topic_tag` | Internal category label, distinct wording from the title — could work as a "Topic" badge on the post card, not meant to be keyword-stuffed |
| `word_count` | Just informational (e.g. reading-time estimate) |
| `updated_at` | Last time the post was created or edited — use for `<lastmod>` and `dateModified` |
| `expires_at` | Normally `null`. Only set when an admin deliberately gives a post an end date |

All of these can be `null` on the 2 original posts until the one-time DB
migration backfills them, and can theoretically be `null` on any
manually-created admin post — always code defensively (fall back to `title`
if `meta_title` is null, etc.), don't assume they're always populated.

## Sitemap / SEO

`GET /sitemap.xml` includes every published post, with `<lastmod>` taken
from `updated_at`.

## Behavior to design around

- **Post URLs are stable.** A published post keeps its slug for good; it only
  goes away if an admin unpublishes or deletes it.
- **A post's content can change.** The generator may refresh an existing post
  on the same topic rather than add a new one - `updated_at` moves when it
  does.
- **New posts appear about once a week**, occasionally more if an admin
  manually triggers one. Each one triggers a frontend rebuild.

## Admin-only endpoints (if you're building the admin panel)

All under `/api/admin`, require an admin bearer token (`Authorization:
Bearer <access_token>`), same auth pattern as the rest of `/api/admin/*`.

### `GET /api/admin/blog` — all posts (published, unpublished, drafts), for a management table

Each post in the admin responses also carries `warnings: string[]`. It is
non-empty when the post is under 800 words - show it next to the post; it
does not block publishing.

### `POST /api/admin/blog` / `PUT /api/admin/blog/{id}` / `DELETE /api/admin/blog/{id}`
Manual CRUD, unchanged from before except the request/response bodies now
also accept the SEO fields above. Notes:

- `expires_at` is optional; `null` (the default) means the post never expires.
- `word_count` is recalculated from `content` on every save.
- `409` if `meta_title` or `meta_description` matches another published post.
- `400` if you try to change the `slug` of a post that has been published.
- Publishing, editing, unpublishing or deleting a live post triggers a
  frontend rebuild.

### `POST /api/admin/blog/generate`
Manually fires the automated generation pipeline right now (bypasses the
weekly cooldown) — useful for a "Generate now" button if the admin panel
wants one. Always returns `200`, never a generation-related error status:
```json
{ "success": true, "detail": "Blog post generated and published", "post_id": "uuid", "slug": "..." }
```
`detail` says whether a new post was created or an existing post on the same
topic was updated instead (`slug` is then that existing post's slug). If both
LLM providers are temporarily down:
```json
{ "success": false, "detail": "All configured LLM providers failed to generate a response" }
```
Treat `success: false` as "try again shortly," not a bug report.

### `GET /api/admin/blog/generation-logs?limit=20`
Recent generation attempts (success/failure, which LLM was used, topic,
error message) — useful for an admin diagnostics view.

## Testing checklist

1. `GET /api/blog` returns every published post, each with `updated_at`.
2. `GET /api/blog/{slug}` returns `200` for a published slug and `404` for an
   unpublished or made-up one.
3. Publishing a post in the admin triggers a Vercel deployment.
