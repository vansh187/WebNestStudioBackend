# AI Page Builder — "Something went wrong" on generate/refine

**Status:** Diagnosed. Fix is frontend-only, no backend change required.
**Affects:** `src/lib/apiClient.js`

## Symptom

Users see a generic red "Something went wrong. Please try again." banner when
generating or refining an AI-built page. It doesn't happen on every attempt —
more often after the app has been open for a bit ("warm").

## Root cause

`apiClient.js` applies a single global request timeout to every API call:

```js
const COLD_START_TIMEOUT = 60000
const WARM_TIMEOUT = 10000
...
config.timeout = hasCompletedFirstRequest ? WARM_TIMEOUT : COLD_START_TIMEOUT
```

Once the first request of the session succeeds, every subsequent call — including
`/api/generate` and `/api/generate/{id}/refine` — gets a **10 second** timeout.

That's fine for ordinary CRUD calls, but AI page generation is inherently
slower: the backend calls Gemini (and falls back to Groq if Gemini fails),
with up to `LLM_TIMEOUT_SECONDS` (20s) allowed per provider attempt on our
side, before any network/DB overhead. A normal successful generation can
easily take 10–20+ seconds.

Axios cancels the request client-side at the 10s mark. The backend is still
working and does return a valid response — it's just too late, the browser
already aborted the connection. `getErrorDetail()`'s fallback string
(`"Something went wrong. Please try again."`) fires because a canceled
request has no `error.response` to read a real message from.

### How this was confirmed

- Chrome DevTools Network tab showed the `generate` XHR with status
  **`(canceled)`** at **10.01s** — a client-side abort, not a server error
  status.
- The Render backend log for that exact timestamp shows Gemini returned
  `200 OK` — the backend was mid-flight, working correctly, when the browser
  gave up on it.

## Fix

Give the AI Page Builder endpoints their own, longer timeout instead of
falling under `WARM_TIMEOUT`. Suggested change to `apiClient.js`:

```js
const AI_BUILDER_PATHS = ['/api/generate'] // matches /api/generate and /api/generate/{id}/refine
const AI_BUILDER_TIMEOUT = 45000 // LLM latency + a possible Gemini->Groq fallback can exceed 10s easily

api.interceptors.request.use((config) => {
  const isAiBuilderCall = AI_BUILDER_PATHS.some((p) => config.url?.includes(p))
  config.timeout = isAiBuilderCall
    ? AI_BUILDER_TIMEOUT
    : hasCompletedFirstRequest
      ? WARM_TIMEOUT
      : COLD_START_TIMEOUT

  const token = getAccessToken()
  if (token) config.headers.Authorization = `Bearer ${token}`

  pendingSlowCount += 1
  config._slowTimer = setTimeout(() => notifySlow(), SLOW_REQUEST_THRESHOLD)

  return config
})
```

`45000` gives comfortable headroom above the worst case (two ~20s provider
attempts plus overhead) without leaving users staring at a spinner
indefinitely if something is genuinely broken.

## Testing after the fix

1. Open the AI Page Builder chat, submit a prompt, and let the app sit idle
   for a minute or two first so `hasCompletedFirstRequest` is already `true`
   (i.e. you're in the "warm" timeout path, which is what broke before).
2. Submit a generation prompt and confirm it completes instead of showing
   the generic error around the 10s mark.
3. In DevTools → Network, confirm the `generate` request shows a real status
   code (`200`, or a real `429`/`503` if applicable) instead of `(canceled)`.
4. Repeat for a refinement call (`/api/generate/{id}/refine`).

## Not the cause (ruled out during investigation)

- **Rate limiting** — the account's generation history showed several
  successful generations shortly before the failure, which raised this as a
  suspect, but the canceled-at-10.01s network trace and matching Gemini
  `200 OK` in the backend log at the same timestamp confirm the request was
  still in flight, not rejected. `RATE_LIMIT_PER_HOUR` was independently
  raised to `100` as a precaution but was not the actual cause here.
- **Backend errors** — the backend log shows no error for the request in
  question; Gemini responded successfully.
