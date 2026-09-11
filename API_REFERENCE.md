# WebNest Studio API Reference (for Frontend Integration)

**Base URL:** `https://webneststudiobackend-n00h.onrender.com`

All paths below are relative to this base URL. All request/response bodies are JSON (`Content-Type: application/json`), except where noted.

> **Note:** This is a free Render instance, so the first request after idle time can take 30-50 seconds while the server spins up ("cold start"). Retry once if the first request times out.

> **⚠️ Email verification is temporarily OFF** (sending domain pending). Right now: `POST /api/auth/signup` returns an account with `is_verified: true` immediately, and you can call `POST /api/auth/login` right after signup with no OTP step at all. `verify-otp` / `resend-otp` will return a `422` explaining they're disabled if called. This will be re-enabled once the domain is ready — build the signup → verify-otp → login flow in the UI as normal, it'll just be a no-op for verify-otp until then.

---

## Authentication

Protected endpoints require an `Authorization` header:

```
Authorization: Bearer <access_token>
```

Get `access_token` from `POST /api/auth/login` (or `/api/auth/refresh`). Access tokens expire in **15 minutes** — use the refresh token to get a new one. Refresh tokens last **30 days**.

**Login requires a verified, active account** (currently suspended — see the email-verification note above). If a user hasn't completed OTP verification, login returns `401` once verification is re-enabled.

---

## Error format

Every error response is JSON with a `detail` field:

```json
{ "detail": "Invalid email or password" }
```

Validation errors (missing/malformed fields) return `422` with extra detail:

```json
{
  "detail": "Request validation failed",
  "errors": [
    { "type": "string_too_short", "loc": ["body", "password"], "msg": "String should have at least 8 characters" }
  ]
}
```

| Status | Meaning |
|---|---|
| 200 / 201 | Success |
| 204 | Success, no response body |
| 401 | Missing/invalid/expired token, wrong credentials, unverified/disabled account |
| 403 | Logged in, but wrong role for this endpoint |
| 404 | Resource not found |
| 409 | Conflict (e.g. email/slug already exists) |
| 422 | Request body failed validation |
| 503 | Database temporarily unavailable — retry |
| 500 | Unexpected server error — retry, then report if persistent |

---

## 1. Auth — `/api/auth`

### `POST /api/auth/signup`
No auth required. Creates the account (unverified) and emails a 6-digit OTP.

**Request**
```json
{
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "password": "SecurePass123"
}
```
`phone_number` is optional. `password` must be 8-72 characters.

**Response `201`**
```json
{
  "id": "282232c5-40f1-4247-a660-0e04cd386b78",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "role": "client",
  "is_verified": false,
  "is_active": true,
  "last_login_at": null,
  "created_at": "2026-07-22T14:24:53.611086Z"
}
```

**Error `409`** — email already registered:
```json
{ "detail": "An account with this email already exists" }
```

---

### `POST /api/auth/verify-otp`
No auth required. Confirms the 6-digit code emailed at signup.

**Request**
```json
{
  "email": "jane@example.com",
  "otp_code": "482913",
  "purpose": "signup"
}
```
`purpose` is one of `signup` | `login` | `password_reset` (defaults to `signup`).

**Response `200`**
```json
{
  "id": "282232c5-40f1-4247-a660-0e04cd386b78",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "role": "client",
  "is_verified": true,
  "is_active": true,
  "last_login_at": null,
  "created_at": "2026-07-22T14:24:53.611086Z"
}
```

**Error `422`** — wrong or expired code:
```json
{ "detail": "Invalid or expired OTP code" }
```

---

### `POST /api/auth/resend-otp`
No auth required. Issues a fresh OTP for an account whose original code expired or was lost — use this instead of retrying signup (retrying signup for an existing email returns `409`).

**Request**
```json
{ "email": "jane@example.com", "purpose": "signup" }
```

**Response `200`**
```json
{ "message": "A new verification code has been sent to your email" }
```

**Error `404`** — no account with that email:
```json
{ "detail": "No account found for this email" }
```

**Error `422`** — account already verified (only checked for `purpose: "signup"`):
```json
{ "detail": "This account is already verified" }
```

**Error `429`** — rate-limited to 1 resend per 60 seconds per email+purpose:
```json
{ "detail": "Please wait 42s before requesting another code" }
```

---

### `POST /api/auth/login`
No auth required.

**Request**
```json
{
  "email": "jane@example.com",
  "password": "SecurePass123"
}
```

**Response `200`**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

**Error `401`** — wrong credentials:
```json
{ "detail": "Invalid email or password" }
```

**Error `401`** — not verified yet:
```json
{ "detail": "Please verify your email address before logging in" }
```

---

### `POST /api/auth/refresh`
No auth header — the refresh token itself is the credential. Rotates the refresh token (the old one is invalidated).

**Request**
```json
{ "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." }
```

**Response `200`**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

**Error `401`**:
```json
{ "detail": "Refresh token has expired" }
```

---

### `POST /api/auth/logout`
No bearer header required — pass the refresh token to revoke it.

**Request**
```json
{ "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." }
```

**Response `204`** — empty body.

---

### `GET /api/auth/me`
Requires `Authorization: Bearer <access_token>`.

**Response `200`**
```json
{
  "id": "282232c5-40f1-4247-a660-0e04cd386b78",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "role": "client",
  "is_verified": true,
  "is_active": true,
  "last_login_at": "2026-07-22T14:25:31.651698Z",
  "created_at": "2026-07-22T14:24:53.611086Z"
}
```

**Error `401`** — missing/expired token:
```json
{ "detail": "Missing bearer token" }
```

### `DELETE /api/auth/me`
Requires `Authorization: Bearer <access_token>`. Use this for Android and website account-removal flows.

The backend deletes the current user account, revokes all refresh tokens, removes directly email-linked records, and lets database cascades remove user-owned app data.

**Request**
No body. Requests with any body are rejected with `422`.

```http
DELETE /api/auth/me
Authorization: Bearer <access_token>
```

**Response `204`** - empty body.

**Error `401`** - missing/expired token:
```json
{ "detail": "Missing bearer token" }
```

---

## 2. Lead Capture — `/api/leads`

One endpoint for **every** form on the site: Contact form, Start a Project, consultation booking, resource download. Differentiate using `source`. The server captures `ip_address`/`user_agent` itself — don't send them, they're ignored either way.

### `POST /api/leads`
No auth required.

`source` must be one of: `contact_form`, `start_project`, `consultation_booking`, `resource_download`, `newsletter`, `chatbot`.

**Per-source required fields:**
| source | Required fields |
|---|---|
| `contact_form` | `full_name`, `email`, `phone_number`, `message`, `consent_given: true` |
| `consultation_booking` | `preferred_date`, `preferred_time_slot` |
| `resource_download` | `resource_name` |
| `start_project` / `newsletter` / `chatbot` | no extra required fields |

**Request — Contact form**
```json
{
  "source": "contact_form",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "message": "I need a quote for an e-commerce site.",
  "consent_given": true
}
```

**Request — Start a Project**
```json
{
  "source": "start_project",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "project_type": "Web App",
  "budget_range": "$10k-$25k",
  "timeline": "3 months",
  "message": "We need a customer portal.",
  "consent_given": true,
  "utm_source": "google",
  "utm_medium": "cpc",
  "utm_campaign": "spring-launch"
}
```

**Request — Consultation booking**
```json
{
  "source": "consultation_booking",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "preferred_date": "2026-08-01",
  "preferred_time_slot": "10:00-10:30 IST",
  "consent_given": true
}
```

**Request — Resource download**
```json
{
  "source": "resource_download",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "resource_name": "2026 Web Dev Trends Whitepaper",
  "consent_given": true
}
```

**Response `201`** (same shape for every source)
```json
{
  "id": "ce3e839a-bfe0-46ee-a6f5-163e09b4bbc6",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "9876543210",
  "source": "contact_form",
  "project_type": null,
  "budget_range": null,
  "timeline": null,
  "preferred_date": null,
  "preferred_time_slot": null,
  "resource_name": null,
  "message": "I need a quote for an e-commerce site.",
  "brief_file_url": null,
  "utm_source": null,
  "utm_medium": null,
  "utm_campaign": null,
  "referrer_url": null,
  "status": "new",
  "consent_given": true,
  "created_at": "2026-07-22T14:25:38.322683Z",
  "updated_at": "2026-07-22T14:25:38.322683Z"
}
```

**Error `422`** — missing required field for the source:
```json
{ "detail": "Request validation failed", "errors": [{"type": "value_error", "msg": "Value error, Missing required field(s) for contact_form: phone_number"}] }
```

---

## 3. Newsletter — `/api/newsletter`

### `POST /api/newsletter/subscribe`
No auth required. Idempotent — resubscribes if the email previously unsubscribed.

**Request**
```json
{ "email": "jane@example.com", "source": "footer" }
```

**Response `201`**
```json
{ "email": "jane@example.com", "subscribed": true }
```

---

## 4. Home Page — `/api/home`

All public, no auth.

### `GET /api/home/services-preview?limit=4`
**Response `200`**
```json
[
  {
    "id": "00278354-66ac-41a0-98ad-a91116f71de1",
    "title": "Web Development",
    "slug": "web-development",
    "short_description": "Modern, fast websites.",
    "full_description": null,
    "icon_url": "https://cdn.example.com/icons/web.svg",
    "tech_tags": ["React", "Next.js"],
    "display_order": 1,
    "is_published": true
  }
]
```

### `GET /api/home/featured-work?limit=6`
**Response `200`**
```json
[
  {
    "id": "9f1c2b3a-1111-4a2b-9c3d-4e5f6a7b8c9d",
    "title": "Acme Retail Platform",
    "slug": "acme-retail-platform",
    "category": "Web",
    "short_description": "E-commerce replatform.",
    "full_description": null,
    "tech_stack": ["Next.js", "Postgres"],
    "cover_image_url": "https://cdn.example.com/portfolio/acme.jpg",
    "gallery_urls": [],
    "result_metrics": "40% faster checkout, 2x conversion",
    "client_name": "Acme Retail",
    "display_order": 1,
    "is_published": true
  }
]
```

### `GET /api/home/testimonials?limit=6`
**Response `200`**
```json
[
  {
    "id": "5a6b7c8d-2222-4a2b-9c3d-4e5f6a7b8c9d",
    "client_name": "Priya Sharma",
    "company": "Acme Retail",
    "quote": "WebNest Studio delivered ahead of schedule.",
    "rating": 5,
    "avatar_url": "https://cdn.example.com/avatars/priya.jpg",
    "is_published": true
  }
]
```

### `GET /api/home/project-status-demo`
Static demo record for the public "Project Status" widget (not tied to a real client).

**Response `200`**
```json
{
  "project_name": "WebNest Studio Demo Project",
  "phase": "Development",
  "percent_complete": 65
}
```

### `GET /api/home/stats`
**Response `200`**
```json
{ "projects_delivered": 12 }
```

---

## 5. Content (public read) — `/api`

### `GET /api/services`
**Response `200`** — same shape as `services-preview` above, all published services (no `limit`).

### `GET /api/portfolio?category=Web`
`category` query param is optional (`Web`, `AI`, `Enterprise`).
**Response `200`** — array, same shape as `featured-work` above.

### `GET /api/portfolio/{slug}`
**Response `200`** — single object, same shape as one item above.
**Error `404`**:
```json
{ "detail": "Portfolio item not found" }
```

### `GET /api/blog?tag=nextjs`
`tag` query param is optional.

**Response `200`**
```json
[
  {
    "id": "1a2b3c4d-3333-4a2b-9c3d-4e5f6a7b8c9d",
    "title": "5 Ways AI Is Changing Web Development",
    "slug": "ai-changing-web-development",
    "excerpt": "A look at how AI tooling speeds up delivery.",
    "content": "Full markdown/HTML content here...",
    "cover_image_url": "https://cdn.example.com/blog/ai.jpg",
    "author_id": "282232c5-40f1-4247-a660-0e04cd386b78",
    "tags": ["AI", "Web Dev"],
    "is_published": true,
    "published_at": "2026-06-01T09:00:00Z"
  }
]
```

### `GET /api/blog/{slug}`
**Response `200`** — single object, same shape as above.
**Error `404`**:
```json
{ "detail": "Blog post not found" }
```

### `GET /api/faqs?category=Pricing`
`category` query param is optional.

**Response `200`**
```json
[
  {
    "id": "2b3c4d5e-4444-4a2b-9c3d-4e5f6a7b8c9d",
    "question": "How long does a typical project take?",
    "answer": "4-12 weeks depending on scope.",
    "category": "Process",
    "display_order": 1,
    "is_published": true
  }
]
```

---

## 6. Client Portal — `/api/me`

Requires `Authorization: Bearer <access_token>` for a `client` (or `admin`) role.

### `GET /api/me/project-status`
**Response `200`**
```json
{
  "id": "3c4d5e6f-5555-4a2b-9c3d-4e5f6a7b8c9d",
  "client_user_id": "282232c5-40f1-4247-a660-0e04cd386b78",
  "project_name": "Acme Retail Platform",
  "phase": "Development",
  "percent_complete": 65,
  "updated_at": "2026-07-20T10:00:00Z"
}
```
**Error `404`** — admin hasn't set one up yet:
```json
{ "detail": "No project status found for this client yet" }
```

### `GET /api/me/files`
Placeholder until Supabase Storage file listing is wired up.

**Response `200`**
```json
[]
```

---

## 7. Admin — `/api/admin`

Requires `Authorization: Bearer <access_token>` for an `admin` role. All endpoints below return `403` with `{"detail": "Insufficient permissions"}` for non-admins.

### Leads (CRM-lite)

#### `GET /api/admin/leads?source=contact_form&status_filter=new&limit=50&offset=0`
All query params optional. `limit` max 200.

**Response `200`**
```json
{
  "total": 2,
  "items": [
    {
      "id": "ce3e839a-bfe0-46ee-a6f5-163e09b4bbc6",
      "full_name": "Jane Doe",
      "email": "jane@example.com",
      "phone_number": "9876543210",
      "source": "contact_form",
      "project_type": null,
      "budget_range": null,
      "timeline": null,
      "preferred_date": null,
      "preferred_time_slot": null,
      "resource_name": null,
      "message": "I need a quote.",
      "brief_file_url": null,
      "utm_source": null,
      "utm_medium": null,
      "utm_campaign": null,
      "referrer_url": null,
      "status": "new",
      "consent_given": true,
      "created_at": "2026-07-22T14:25:38.322683Z",
      "updated_at": "2026-07-22T14:25:38.322683Z"
    }
  ]
}
```

#### `PUT /api/admin/leads/{lead_id}`
**Request**
```json
{ "status": "contacted" }
```
`status` is one of `new`, `contacted`, `qualified`, `confirmed`, `completed`, `won`, `lost`.

**Response `200`** — updated lead object (same shape as above).

---

### Services catalog

#### `GET /api/admin/services` — all services (published + unpublished), same shape as public `/api/services`.

#### `POST /api/admin/services`
**Request**
```json
{
  "title": "Web Development",
  "slug": "web-development",
  "short_description": "Modern, fast websites.",
  "full_description": "Full detail here...",
  "icon_url": "https://cdn.example.com/icons/web.svg",
  "tech_tags": ["React", "Next.js"],
  "display_order": 1,
  "is_published": true
}
```
`slug` must be lowercase letters/numbers/dashes only (e.g. `web-development`).

**Response `201`** — created object.
**Error `409`** — duplicate slug:
```json
{ "detail": "A service with this slug already exists" }
```

#### `PUT /api/admin/services/{service_id}`
**Request** (all fields optional, only send what changes)
```json
{ "is_published": false }
```
**Response `200`** — updated object.

#### `DELETE /api/admin/services/{service_id}`
**Response `204`** — empty body.

---

### Portfolio items — `/api/admin/portfolio`
Same CRUD pattern as services.

**`POST` request**
```json
{
  "title": "Acme Retail Platform",
  "slug": "acme-retail-platform",
  "category": "Web",
  "short_description": "E-commerce replatform.",
  "tech_stack": ["Next.js", "Postgres"],
  "cover_image_url": "https://cdn.example.com/portfolio/acme.jpg",
  "gallery_urls": ["https://cdn.example.com/portfolio/acme-2.jpg"],
  "result_metrics": "40% faster checkout, 2x conversion",
  "client_name": "Acme Retail",
  "display_order": 1,
  "is_published": true
}
```
`GET /api/admin/portfolio`, `PUT /api/admin/portfolio/{item_id}`, `DELETE /api/admin/portfolio/{item_id}` all work the same way as services.

---

### Testimonials — `/api/admin/testimonials`

**`POST` request**
```json
{
  "client_name": "Priya Sharma",
  "company": "Acme Retail",
  "quote": "WebNest Studio delivered ahead of schedule.",
  "rating": 5,
  "avatar_url": "https://cdn.example.com/avatars/priya.jpg",
  "is_published": true
}
```
`rating` must be an integer 1-5. `GET`/`PUT`/`DELETE` follow the same pattern.

---

### FAQs — `/api/admin/faqs`

**`POST` request**
```json
{
  "question": "How long does a typical project take?",
  "answer": "4-12 weeks depending on scope.",
  "category": "Process",
  "display_order": 1,
  "is_published": true
}
```
`GET`/`PUT`/`DELETE` follow the same pattern.

---

### Blog posts — `/api/admin/blog`

**`POST` request**
```json
{
  "title": "5 Ways AI Is Changing Web Development",
  "slug": "ai-changing-web-development",
  "excerpt": "A look at how AI tooling speeds up delivery.",
  "content": "Full markdown/HTML content here...",
  "cover_image_url": "https://cdn.example.com/blog/ai.jpg",
  "tags": ["AI", "Web Dev"],
  "is_published": true,
  "published_at": "2026-06-01T09:00:00Z"
}
```
`GET`/`PUT`/`DELETE` follow the same pattern.

---

### Client project status

#### `PUT /api/admin/project-status/{client_user_id}`
Upserts (creates or updates) a client's project status.

**Request**
```json
{
  "project_name": "Acme Retail Platform",
  "phase": "Development",
  "percent_complete": 65
}
```
`phase` is a free-text label, e.g. `Discovery`, `Design`, `Development`, `AI Integration`, `Launch`.

**Response `200`**
```json
{
  "id": "3c4d5e6f-5555-4a2b-9c3d-4e5f6a7b8c9d",
  "client_user_id": "282232c5-40f1-4247-a660-0e04cd386b78",
  "project_name": "Acme Retail Platform",
  "phase": "Development",
  "percent_complete": 65,
  "updated_at": "2026-07-22T10:00:00Z"
}
```

---

### Email diagnostics

#### `POST /api/admin/test-email`
Synchronously attempts a real send via Resend and reports the actual result — unlike signup/lead-capture (where email is fire-and-forget in the background), this endpoint waits for the attempt so you can verify email delivery end-to-end without needing server log access.

**Request** (`to_address` optional — defaults to the configured `RESEND_FROM_ADDRESS`)
```json
{ "to_address": "someone@example.com" }
```

**Response `200`** — success
```json
{
  "sent": true,
  "to_address": "someone@example.com",
  "provider": "resend",
  "from_address": "onboarding@resend.dev",
  "api_key_configured": true,
  "detail": "Email sent successfully."
}
```

**Response `200`** — failure (the `detail` field carries Resend's own error message, e.g. sandbox-mode recipient restriction)
```json
{
  "sent": false,
  "to_address": "someone@example.com",
  "provider": "resend",
  "from_address": "onboarding@resend.dev",
  "api_key_configured": true,
  "detail": "Resend API returned HTTP 403: {\"statusCode\":403,\"name\":\"validation_error\",\"message\":\"You can only send testing emails to your own email address...\"}"
}
```
Note: with the sandbox sender (`onboarding@resend.dev`), Resend only delivers to the email address the Resend account itself was registered with — every other recipient gets rejected until a domain is verified at resend.com/domains.

---

## 8. Health check

### `GET /health`
No auth. Use for uptime monitors / to "wake up" the Render instance before a demo.

**Response `200`**
```json
{ "status": "ok" }
```

---

## Quick integration checklist

1. Store `access_token` in memory (not localStorage if you can help it) and `refresh_token` in an httpOnly cookie or secure storage.
2. On `401` from any authenticated call, try `POST /api/auth/refresh` once, then retry the original request. If refresh also fails, force re-login.
3. Every public form on the site posts to `POST /api/leads` — just switch the `source` value per form (see the table in section 2).
4. Interactive Swagger docs are also live at `https://webneststudiobackend-n00h.onrender.com/docs` if you want to try requests directly in the browser.
