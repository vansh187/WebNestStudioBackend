# Sitemap & Robots — Notes for the Frontend Team

The backend now serves `sitemap.xml` and `robots.txt` dynamically. This is a
backend-owned concern — there is nothing to build in the React app — but a
few things affect how these get deployed on `webneststudio.co.in`.

## What's live

| Endpoint       | Served by | Content-Type       |
|----------------|-----------|---------------------|
| `/sitemap.xml` | FastAPI backend | `application/xml` |
| `/robots.txt`  | FastAPI backend | `text/plain`       |

Both are unauthenticated, public GET routes. Source: `api/sitemap_router.py`.

## What's in the sitemap

- **Static pages**, matching the routes in `App.jsx`:
  `/`, `/about`, `/services`, `/portfolio`, `/blog`, `/faqs`, `/contact`
  (`/login`, `/portal`, `/admin` are intentionally excluded — not public/indexable)
- **Blog posts** — every row in `blog_posts` where `is_published = true`, as
  `/blog/{slug}`, with `<lastmod>` from `updated_at` (falls back to `published_at`)
- **Portfolio items** — every row in `portfolio_items` where `is_published = true`,
  as `/portfolio/{slug}`, with `<lastmod>` from `updated_at`

All URLs are absolute, built from `frontend_base_url`
(`https://webneststudio.co.in` by default, backend env var).

**If you add a new public top-level route** (e.g. `/pricing`), it won't
automatically appear in the sitemap — ping the backend to add it to the
`STATIC_PATHS` list in `api/sitemap_router.py`.

## The one thing that needs your/DevOps attention

This backend has no static file serving and no SPA catch-all route, so
nothing in the FastAPI app itself can intercept these two paths. **But** if
`webneststudio.co.in` sits behind a CDN, reverse proxy, or static hosting
provider (Vercel/Netlify/nginx/etc.) that serves the React build for
everything under `/`, that layer needs an explicit rule to route
`/sitemap.xml` and `/robots.txt` to the FastAPI backend instead of falling
through to `index.html`.

Quick way to check if it's misconfigured in production:

```bash
curl -i https://webneststudio.co.in/sitemap.xml
curl -i https://webneststudio.co.in/robots.txt
```

Expect `Content-Type: application/xml` / `text/plain` and XML/plain-text
bodies. If you see `Content-Type: text/html` or the React app's
`<!DOCTYPE html>`, the routing layer in front of the domain is swallowing
these paths before they reach the backend.
