# WebNest Studio Backend

FastAPI backend for the WebNest Studio website: auth, lead capture/CRM, public content (services, portfolio, blog, testimonials, FAQs), newsletter, and a client portal. Built against Supabase Postgres via SQLAlchemy (async) + asyncpg, with custom JWT auth and email OTP verification.

## Architecture

The codebase is organized into loosely-coupled packages, each with one responsibility. Every class is instance-based (no static methods) and dependencies are constructed explicitly and passed in (constructor injection), so each layer can be tested or swapped independently.

```
app.py                  # FastAPI app assembly (WebNestStudioApp), routers, middleware, lifespan
core/                   # cross-cutting concerns
  config.py             # Settings (env/.env loaded), async_database_url helper
  security.py           # PasswordHasher, JWTHandler, OtpGenerator
  dependencies.py        # DependencyContainer + FastAPI Depends() wiring, RoleChecker
  exceptions.py          # DomainError hierarchy (NotFoundError, ConflictError, ...)
  error_handlers.py      # maps DomainError -> HTTP status codes
database/               # all persistence logic lives here (files suffixed *_persistence.py)
  session.py             # Database class: async engine + session factory
  models.py              # SQLAlchemy ORM models (one per table in the schema doc)
  user_persistence.py, refresh_token_persistence.py, otp_persistence.py
  lead_persistence.py, newsletter_persistence.py
  service_persistence.py, portfolio_persistence.py, testimonial_persistence.py,
  faq_persistence.py, blog_persistence.py, project_status_persistence.py
services/               # business logic (files suffixed *_service.py)
  auth_service.py, email_service.py, lead_service.py, newsletter_service.py
  service_catalog_service.py, portfolio_service.py, testimonial_service.py,
  faq_service.py, blog_service.py, client_service.py
schemas/                # Pydantic request/response models
  auth_schemas.py, lead_schemas.py, newsletter_schemas.py, content_schemas.py, client_schemas.py
api/                    # FastAPI routers (thin: validate -> call service -> return)
  auth_router.py, leads_router.py, newsletter_router.py, content_router.py,
  home_router.py, admin_router.py, client_router.py
```

**Request flow**: `api/*_router.py` (HTTP concerns only) -> `services/*_service.py` (business rules) -> `database/*_persistence.py` (SQL via SQLAlchemy). Routers never talk to the database directly; services never build HTTP responses.

Domain errors (`NotFoundError`, `ConflictError`, `UnauthorizedError`, `ForbiddenError`, `ValidationError` in `core/exceptions.py`) are raised from services and translated to HTTP status codes centrally in `core/error_handlers.py`, so routers stay free of try/except boilerplate.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Configure `.env` (already present, fill in the blanks):

```
SUPABASE_URL=<Supabase Session Pooler connection string>
JWT_SECRET_KEY=<your secret>
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
OTP_EXPIRE_MINUTES=10

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_APP_PASSWORD=
SMTP_FROM_ADDRESS=
TEAM_NOTIFICATION_EMAIL=

CORS_ORIGINS=*
```

- `SUPABASE_URL` may be given with or without a `postgresql://` scheme — `core/config.py` normalizes it and rewrites it to the `postgresql+asyncpg://` driver automatically.
- SMTP is optional in development: if `SMTP_USER`/`SMTP_APP_PASSWORD` are blank, or the send fails for any reason, `EmailService` logs a warning and the triggering request still succeeds (signup/lead creation are never blocked by email delivery).
- `CORS_ORIGINS` accepts `*` or a comma-separated list of allowed frontend origins.

Run the API:

```bash
uvicorn app:app --reload
```

On startup, the app connects to Supabase and creates any missing tables (`Base.metadata.create_all`) — no separate migration step is required for this stage of the project. Interactive API docs are available at `/docs` (Swagger) and `/redoc` once running.

Health check: `GET /health` -> `{"status": "ok"}`.

## Authentication

- Signup issues a 6-digit email OTP (10 min expiry) and creates the user as unverified.
- `POST /api/auth/verify-otp` marks the account verified.
- Login returns a short-lived access token (15 min, JWT) and a long-lived refresh token (30 days). The refresh token is only ever stored server-side as a SHA-256 hash (`refresh_tokens` table) and rotates on every `/api/auth/refresh` call.
- **Login requires a verified, active account** — an unverified email or a disabled (`is_active=false`) account gets a 401 with a specific message rather than a token.
- Protected endpoints expect `Authorization: Bearer <access_token>`.
- Roles: `client`, `admin`, `editor` (only `client`/`admin` are enforced by route guards currently — see `core/dependencies.py: RoleChecker`).

## API Endpoints

### Auth (`/api/auth`)
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/signup` | none | creates user, sends OTP |
| POST | `/verify-otp` | none | verifies email, activates account |
| POST | `/login` | none | returns access + refresh JWT |
| POST | `/refresh` | none | rotates refresh token |
| POST | `/logout` | none | revokes the given refresh token |
| GET | `/me` | bearer | current user profile |

### Leads (`/api/leads`, `/api/newsletter`)
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/leads` | none | single endpoint for contact form, start-a-project, consultation booking, resource download — differentiated by `source`; server captures `ip_address`/`user_agent`, never trusts client-supplied values for those two fields |
| POST | `/api/newsletter/subscribe` | none | idempotent subscribe/resubscribe |

### Home (`/api/home`)
`GET /services-preview`, `GET /featured-work`, `GET /testimonials`, `GET /project-status-demo`, `GET /stats` — all public, power the homepage.

### Content (public read)
`GET /api/services`, `GET /api/portfolio` (filter `?category=`), `GET /api/portfolio/{slug}`, `GET /api/blog` (filter `?tag=`), `GET /api/blog/{slug}`, `GET /api/faqs` (filter `?category=`).

### Admin (`/api/admin`, requires bearer token with `role=admin`)
- `GET/PUT /leads` — CRM-lite view, filter by `?source=&status_filter=`, update status
- Full CRUD (`GET` list, `POST` create, `PUT` update, `DELETE`) on `/services`, `/portfolio`, `/testimonials`, `/faqs`, `/blog`
- `PUT /project-status/{client_user_id}` — upserts a client's project status

### Client Portal (`/api/me`, requires bearer token with `role=client` or `admin`)
- `GET /project-status`
- `GET /files` — placeholder, returns `[]` until Supabase Storage file listing is wired up (no `files` table exists in the schema yet)

## Error Handling

No request can crash the process with a raw traceback — every layer funnels failures into a typed exception that a global handler turns into a clean JSON response.

- **`core/exceptions.py`** defines the domain error hierarchy: `NotFoundError` (404), `ConflictError` (409), `UnauthorizedError` (401), `ForbiddenError` (403), `ValidationError` (422), `DatabaseError` (503). Services raise these; routers never contain `try/except`.
- **`database/base_persistence.py`** — every `*_persistence.py` class extends `BasePersistence` and routes its queries/commits through `_execute`/`_commit`/`_refresh`/`_delete`. These catch `IntegrityError` (unique constraint violations -> `ConflictError` with a specific message, e.g. "A service with this slug already exists") and any other `SQLAlchemyError` (connection drops, timeouts -> `DatabaseError`, rolling back the session first).
- **`core/security.py`** — `PasswordHasher`, `JWTHandler` and `OtpGenerator` validate their inputs and catch library-level failures (`PasslibSecurityError`, `JWTError`), converting them into plain `ValueError`s with actionable messages instead of letting bcrypt/jose internals leak out. `services/auth_service.py` catches those and re-raises as the appropriate domain error.
- **`services/email_service.py`** — SMTP is best-effort: a bad app password, DNS failure, or timeout is logged and swallowed so signup/lead-capture never fails because of a downstream email problem.
- **`core/error_handlers.py`** registers handlers for every domain error, for `RequestValidationError` (bad request bodies -> 422 with a field-level `errors` list), for `SQLAlchemyError` (any DB exception that slips past the persistence layer -> 503), and a catch-all `Exception` handler (-> 500 with a generic message, full traceback logged server-side only — internals are never echoed to the client).
- **`app.py` lifespan** wraps the startup DB connection so a bad `SUPABASE_URL` logs one clear "check your .env" message instead of a raw asyncpg traceback.

### Corner cases specifically guarded against
- Duplicate signup email, duplicate slug on services/portfolio/blog, duplicate newsletter subscribe (all -> 409, not a DB crash).
- Login before email verification, login for a disabled (`is_active=false`) account, wrong password, expired/revoked/reused refresh token, malformed/expired JWT, missing bearer token.
- Password longer than bcrypt's 72-byte hash limit is rejected at the schema layer (422) before it ever reaches bcrypt.
- OTP: wrong code, expired code, already-consumed code, unknown email — all a single clear `ValidationError`, never a stack trace on a bad guess.
- Lead capture: unknown `source` value, and per-source required fields (e.g. `contact_form` needs `full_name`/`email`/`phone_number`/`message`/`consent_given`; `consultation_booking` needs a date + time slot; `resource_download` needs a resource name) are validated before hitting the database.
- Pagination (`limit`/`offset` on admin leads, `limit` on home preview endpoints) is bounded server-side so a client can't request an unbounded result set.
- Slugs are validated against a strict `lowercase-with-dashes` pattern so they're always safe to use directly in a frontend URL.
- `ip_address`/`user_agent` on leads are always read from request headers server-side — client-submitted values for these fields are never trusted, even if sent.

## Database

Tables mirror `webnest-studio-backend-schema.md`: `users`, `refresh_tokens`, `otp_verifications`, `leads`, `newsletter_subscribers`, `services`, `portfolio_items`, `testimonials`, `faqs`, `blog_posts`, `project_status`. All primary keys are UUIDs (`gen_random_uuid()`), foreign keys into `users` are nullable outside the auth module itself, and `leads`/`portfolio_items` use soft delete (`is_deleted`) rather than hard delete.

## Notes for the frontend team

- All list/detail content endpoints are public (no auth) and only ever return published rows.
- The lead capture endpoint is intentionally generic — send the right `source` value (`contact_form`, `start_project`, `consultation_booking`, `resource_download`, `newsletter`, `chatbot`) and only the fields relevant to that flow.
- Admin and client-portal routes require the `Authorization: Bearer <access_token>` header obtained from `/api/auth/login`.
