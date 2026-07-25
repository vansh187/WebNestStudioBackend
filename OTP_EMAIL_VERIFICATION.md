# OTP Email Verification — Notes for the Frontend Team

**Status:** Live and confirmed working in production — OTP emails are
delivering to real inboxes (not just the Resend sandbox address).

## What changed

`EMAIL_ENABLED` is now `true` in production. This is a real behavior change,
not just a cosmetic email update:

- **Before:** signup auto-verified accounts instantly (`is_verified: true`
  right away) and OTP verification was fully skipped.
- **Now:** signup creates the account as **unverified**, sends a real OTP
  email, and **login is blocked until the account is verified**.

If the frontend doesn't already have an OTP verification screen wired up,
it needs one now — otherwise users who sign up will get stuck unable to log
in, with no way to see why unless the 401 error message is surfaced.

Also new: the OTP email itself is now a proper branded HTML email (three
lines about WebNest Studio, the code highlighted in a green box, logo
underneath) instead of plain text. That part is entirely backend-owned —
nothing to build for it, just useful context if you see it in a screenshot.

## Root cause of the earlier "OTP never arrives" issue

Not a frontend concern, but recorded here in case it comes up: OTP emails
were failing silently for every real user because `RESEND_FROM_ADDRESS` was
unset/misconfigured on Render, so `email_service.py` fell back to
`onboarding@resend.dev` — Resend's shared sandbox sender, which only
delivers to the email address tied to the Resend account itself. That's why
testing via Swagger with the account owner's own inbox appeared to work
while every other signup silently got nothing. Fixed by verifying
`webneststudio.co.in` with Resend (SPF/DKIM/MX records at the registrar)
and setting `RESEND_FROM_ADDRESS=noreply@webneststudio.co.in` on Render.

## The flow

```
POST /api/auth/signup
  -> account created, is_verified: false
  -> OTP email sent in the background

  ... user enters the 6-digit code from their email ...

POST /api/auth/verify-otp
  -> is_verified: true

POST /api/auth/login
  -> now succeeds
```

If the user tries to log in before verifying, they get a `401` with a
specific message (see below) — that's the signal to route them to the OTP
screen instead of a generic "wrong password" error.

## Endpoints

All under `/api/auth`, no auth header required for any of these.

### `POST /api/auth/signup`

**Request**
```json
{
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "+1234567890",
  "password": "MinimumEightChars"
}
```
`full_name` and `phone_number` are optional. `password`: 8–72 characters.

**Response — `201`**
```json
{
  "id": "uuid",
  "full_name": "Jane Doe",
  "email": "jane@example.com",
  "phone_number": "+1234567890",
  "role": "client",
  "is_verified": false,
  "is_active": true,
  "last_login_at": null,
  "created_at": "2026-07-25T12:00:00Z"
}
```
`is_verified: false` is expected and normal now — don't treat it as an
error. The OTP email is sent asynchronously right after this responds, so
it may arrive a couple seconds late.

**Errors:** `409` if the email is already registered, `422` for validation
(bad email format, password too short/long).

---

### `POST /api/auth/verify-otp`

**Request**
```json
{
  "email": "jane@example.com",
  "otp_code": "123456",
  "purpose": "signup"
}
```
`otp_code` must be exactly 6 digits. `purpose` defaults to `"signup"` if
omitted (also accepts `"login"` / `"password_reset"` for future flows, but
only `"signup"` is actually issued anywhere right now).

**Response — `200`**: same `UserResponse` shape as signup, now with
`is_verified: true`.

**Errors:** `422` with `"Invalid or expired OTP code"` — covers wrong code,
already-consumed code, and expired code (codes expire after
`OTP_EXPIRE_MINUTES`, currently 10 minutes) with the same message, so the
UI can't distinguish "wrong" from "expired" — just show a generic error and
offer resend.

---

### `POST /api/auth/resend-otp`

**Request**
```json
{
  "email": "jane@example.com",
  "purpose": "signup"
}
```

**Response — `200`**
```json
{ "message": "A new verification code has been sent to your email" }
```

**Errors:**
- `404` — no account with that email
- `422` — account is already verified (`"This account is already
  verified"`)
- `429` — cooldown in effect. **This one needs specific UI handling**: the
  detail message is like `"Please wait 42s before requesting another
  code"` — a resend button spammed repeatedly should show that message
  and/or disable itself, not treat it as a generic failure. Cooldown is 60s
  between resends.

---

### `POST /api/auth/login`

**Request**
```json
{ "email": "jane@example.com", "password": "MinimumEightChars" }
```

**Response — `200`**: `{ access_token, refresh_token, token_type }` — unchanged.

**Errors:**
- `401` `"Invalid email or password"` — wrong credentials, same as before.
- `401` `"This account has been disabled"` — account deactivated.
- **`401` `"Please verify your email address before logging in"` — NEW.**
  This is the one that needs handling: on this specific message, route the
  user to the OTP verification screen (pre-filled with their email) instead
  of showing a generic login-failed error. Matching on the exact string is
  fragile long-term, but there's no separate error code for this yet — flag
  if you'd rather we add a distinct field for it.

## Testing checklist

1. Sign up with a real email you can check.
2. Confirm the account comes back `is_verified: false`.
3. Confirm the OTP email arrives (check spam folder the first few times —
   the sending domain is newly verified and may not have sender reputation
   built up yet).
4. Try logging in before verifying — confirm you get routed to the OTP
   screen, not a generic error.
5. Enter the code from the email, confirm `verify-otp` returns
   `is_verified: true`.
6. Log in again — should succeed now.
7. Test the resend button: click it twice quickly, confirm the second
   attempt shows the cooldown message instead of failing silently.
