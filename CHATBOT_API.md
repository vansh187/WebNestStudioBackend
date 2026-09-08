# Unified Chatbot API — Frontend Integration Guide

One chat widget, two capabilities. The frontend calls a single set of `/api/chat/*` endpoints. The backend auto-detects, on the **first real message** of a conversation, whether the user wants:

- a **generated HTML page** (existing AI Page Builder), or
- to **enquire about a project**, which ends in a downloadable week-by-week build plan (no pricing shown).

Once a conversation locks into a mode, it stays in that mode for its lifetime. To do the other thing, start a new thread.

All endpoints require a logged-in user (`Authorization: Bearer <access_token>` — same token as the rest of the API).

---

## 1. Start a conversation

```
POST /api/chat/threads
Authorization: Bearer <token>
```

No body required.

**Response `200`**
```json
{
  "thread_id": "b3f1...",
  "mode": "undecided",
  "reply": "Hi! I can build you a page right now, or help you plan out a project - what would you like to do?"
}
```

Show `reply` as the first bot message. `mode` is always `"undecided"` at this point.

---

## 2. Send a message

```
POST /api/chat/threads/{thread_id}/messages
Authorization: Bearer <token>
Content-Type: application/json

{ "message": "Build me a landing page for a coffee shop" }
```

**Response `200`** — shape depends on `mode`. Always check `mode` first.

### While `mode` is still `"undecided"`
The backend classifies intent on this call. Response:
```json
{
  "thread_id": "b3f1...",
  "mode": "undecided" | "page_builder" | "enquiry",
  "reply": "...",
  "html": null,
  "generation_id": null,
  "collected_fields": null,
  "ready_for_plan": null
}
```
- If the message was ambiguous, `mode` stays `"undecided"` and `reply` is a clarifying question — just keep sending messages to the same thread.
- If it resolved, `mode` flips to `"page_builder"` or `"enquiry"` **on this very response** — from then on this thread behaves as described below.

### `mode: "page_builder"`
```json
{
  "thread_id": "b3f1...",
  "mode": "page_builder",
  "reply": "Here's your generated page.",
  "html": "<!DOCTYPE html>...",
  "generation_id": "a921...",
  "collected_fields": null,
  "ready_for_plan": null
}
```
- `html` is the **full HTML document** — render it (e.g. in an iframe/srcdoc) or offer it as a code/download view.
- Every subsequent message in this thread is treated as a **refinement instruction** ("make the header darker") and returns the updated `html` the same way.
- This is exactly the existing AI Page Builder feature (`/api/generate`/`/api/generate/{id}/refine}` still exist unchanged if you were already calling them directly — the new endpoint just wraps the same underlying feature).

### `mode: "enquiry"`
```json
{
  "thread_id": "b3f1...",
  "mode": "enquiry",
  "reply": "Got it - what's the goal of the site?",
  "html": null,
  "generation_id": null,
  "collected_fields": {
    "project_type": "e-commerce store",
    "project_goal": null,
    "pages_features": ["cart", "checkout"],
    "budget_range": null,
    "timeline_expectation": null,
    "contact_name": "Jane Doe",
    "contact_email": "jane@example.com",
    "contact_phone": "+91..."
  },
  "ready_for_plan": false
}
```
- The bot asks one follow-up question per turn until `project_type`, `project_goal`, `pages_features`, `budget_range`, and `timeline_expectation` are all filled in.
- `contact_name` / `contact_email` / `contact_phone` are pre-filled from the logged-in user's account automatically — **never ask the user for these**.
- `collected_fields` values only ever fill in over time — a field that was set will not silently go back to `null` on a later turn.
- **Watch `ready_for_plan`.** When it flips to `true`, show a "Generate my plan" call-to-action.

**UI suggestion:** render `collected_fields` as a running checklist/summary card next to the chat so the user can see what's been captured so far.

---

## 3. Generate the plan (enquiry mode only)

```
POST /api/chat/threads/{thread_id}/generate-plan
Authorization: Bearer <token>
```

No body. Only valid once `ready_for_plan` was `true` on the last message (otherwise `409`, see Errors below).

**Response `200`**
```json
{
  "thread_id": "b3f1...",
  "plan_id": "9e02...",
  "project_type": "e-commerce store",
  "total_weeks": 6,
  "weeks": [
    { "week_range": "Week 1-2", "title": "Discovery & Design", "description": "..." },
    { "week_range": "Week 3-4", "title": "Core Build", "description": "..." }
  ],
  "html_summary": "<div style=\"...\">...</div>",
  "pdf_download_url": "/api/chat/plans/9e02.../pdf",
  "lead_id": "c410..."
}
```
- `weeks` / `total_weeks`: structured data if you want to build your own timeline UI.
- `html_summary`: a ready-to-render branded HTML **fragment** (not a full page — safe to drop straight into the chat transcript, e.g. via `dangerouslySetInnerHTML` / `v-html`). No escaping needed on your end, it's pre-sanitized.
- `pdf_download_url`: relative path — prefix with your API base URL, then hit it as an authenticated `GET` (see below). Show this as a "Download PDF" button.
- **No pricing/cost ever appears anywhere in this response** — by design.
- Calling this again on the same thread (e.g. after continuing the conversation with more detail) regenerates the plan and returns a new `plan_id` — it does **not** create a duplicate lead internally.

---

## 4. Download the plan PDF

```
GET /api/chat/plans/{plan_id}/pdf
Authorization: Bearer <token>
```

**Response `200`** — raw `application/pdf` bytes, `Content-Disposition: attachment; filename="webnest-studio-plan.pdf"`.

This is a real authenticated binary download, not a JSON response — trigger it as a direct browser navigation/`<a href>` with the auth header attached (e.g. `fetch` + `blob()` + `URL.createObjectURL`, since `<a href>` alone can't carry an `Authorization` header), or open it via a short-lived signed link if you'd prefer — ask backend if you need that pattern added.

---

## 5. Email the plan

```
POST /api/chat/plans/{plan_id}/email
Authorization: Bearer <token>
Content-Type: application/json

{ "to_address": "someone@example.com" }
```
`to_address` is optional — omit it (or send `{}`) to email the logged-in user's own account email.

**Response `200`**
```json
{ "sent": true }
```
`sent: false` means email delivery is currently disabled/misconfigured on the backend (not a frontend bug) — show a graceful "we'll email it to you shortly" fallback rather than treating it as an error.

---

## 6. Conversation history (optional, for a "past chats" list)

```
GET /api/chat/threads
Authorization: Bearer <token>
```
```json
{ "threads": [ { "thread_id": "...", "mode": "enquiry", "created_at": "...", "updated_at": "...", "message_count": 8 } ] }
```

```
GET /api/chat/threads/{thread_id}
Authorization: Bearer <token>
```
```json
{
  "thread_id": "...",
  "mode": "enquiry",
  "messages": [
    { "role": "assistant", "content": "Hi! ...", "mode": "undecided", "created_at": "..." },
    { "role": "user", "content": "I want to plan a project", "mode": "enquiry", "created_at": "..." }
  ]
}
```
Use this to resume/replay a past conversation. Note: for `page_builder` threads, `content` on assistant turns is just a short note ("Here's your generated page.") — the actual HTML isn't repeated here; re-fetch it via the existing `/api/history/{generation_id}` endpoint if you need to resume editing a page from history.

---

## 7. Rate limit status (optional, for a "X messages left" indicator)

```
GET /api/chat/limit-status
Authorization: Bearer <token>
```
```json
{ "remaining": 7, "limit": 10, "reset_at": "2026-07-28T14:00:00Z" }
```

---

## Errors

All errors return `{"detail": "human-readable message"}` (or, for validation errors, `{"detail": "...", "errors": [...]}`). Status codes used by this API:

| Status | Meaning | When |
|---|---|---|
| 400 | Invalid input | Empty message, message too long |
| 401 | Not authenticated | Missing/expired token |
| 403 | Not your conversation/plan | `thread_id`/`plan_id` belongs to another user |
| 404 | Not found | Bad `thread_id`/`plan_id` |
| 409 | Not ready | `generate-plan` called before all 5 fields are gathered, or on a thread not in `enquiry` mode |
| 422 | Validation error | Malformed request body (e.g. bad email in `to_address`) |
| 429 | Rate limited | Too many messages/emails in the current hour window — `detail` includes a wait-time hint |
| 503 | Temporarily unavailable | Both AI providers are down/erroring — safe to show a "try again in a moment" message and retry |

**Recommended handling:** treat 429/503 as retryable-with-backoff; treat 409 on `generate-plan` as "keep chatting, not ready yet" rather than a hard error; everything else (400/401/403/404/422) is a real bug/expired-session case.

---

## Quick reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat/threads` | Start a new conversation |
| POST | `/api/chat/threads/{thread_id}/messages` | Send a message (page refinement or enquiry turn, auto-routed) |
| GET | `/api/chat/threads` | List past conversations |
| GET | `/api/chat/threads/{thread_id}` | Get full transcript of one conversation |
| POST | `/api/chat/threads/{thread_id}/generate-plan` | Generate/regenerate the build plan (enquiry mode only) |
| GET | `/api/chat/plans/{plan_id}/pdf` | Download the plan as a branded PDF |
| POST | `/api/chat/plans/{plan_id}/email` | Email the plan PDF |
| GET | `/api/chat/limit-status` | Remaining messages this hour |
