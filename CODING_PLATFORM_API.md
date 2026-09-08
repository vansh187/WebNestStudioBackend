# Coding Platform API

Implemented endpoints:

- `GET /api/compiler/languages`
- `POST /api/compiler/execute`
- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}`
- `PUT /api/projects/{project_id}`
- `DELETE /api/projects/{project_id}`
- `POST /api/shares`
- `GET /api/shares/{share_id}`
- `GET /api/me/coding-stats`

## Compiler Engine

`POST /api/compiler/execute` proxies to JDoodle's hosted sandbox
(https://www.jdoodle.com/compiler-api). Set these environment variables in production:

- `JDOODLE_CLIENT_ID` / `JDOODLE_CLIENT_SECRET`: credentials from the JDoodle Compiler API dashboard (free plan: 200 runs/day)
- `JDOODLE_BASE_URL`: API base, default `https://api.jdoodle.com/v1`
- `COMPILER_REQUEST_TIMEOUT_SECONDS`: API-to-JDoodle HTTP timeout, default `20`
- `COMPILER_OUTPUT_LIMIT_BYTES`: max captured output bytes returned by this API, default `65536`
- `COMPILER_SOURCE_LIMIT_BYTES`: max execution source bytes, default `131072`

When the JDoodle credentials are unset, `/api/compiler/execute` returns `503`.
The API intentionally does not execute untrusted user code inside the FastAPI
process.

JDoodle limitations reflected in the response:
- One entry-point file is executed (the file named `main.*`, else the first file);
  multi-file projects are not compiled together.
- JDoodle merges program output and error text into a single field, so `stdout`
  carries everything, `stderr` is always empty, and `exit_code` / `signal` /
  `compile` are `null`. `status` is `success` unless a timeout is detected or
  JDoodle explicitly reports failure.
- Command-line `args` are ignored (not supported by the JDoodle execute API).
- Pass a plain-integer `version` (e.g. `"0"`) to pin a specific JDoodle
  `versionIndex`; otherwise a sensible default per language is used.

Rate limiting returns HTTP `429` with `{ "detail": "Too many runs, try again in a minute." }`.

## Persistence

Projects and shares use:

- `coding_projects`
- `coding_shares`
- `coding_executions`

`database.create_all()` will create these tables at startup. For explicit
database deployment, apply `migrations/002_coding_platform.sql`.
