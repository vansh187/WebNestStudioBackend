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

`POST /api/compiler/execute` proxies to a separate Piston-compatible sandbox.
Set these environment variables in production:

- `PISTON_BASE_URL`: base URL for the always-on Piston service, for example `https://piston.example.com`
- `COMPILER_REQUEST_TIMEOUT_SECONDS`: API-to-Piston HTTP timeout, default `12`
- `COMPILER_RUN_TIMEOUT_MS`: run timeout sent to Piston, default `5000`
- `COMPILER_COMPILE_TIMEOUT_MS`: compile timeout sent to Piston, default `5000`
- `COMPILER_MEMORY_LIMIT_BYTES`: compile/run memory limit, default `268435456`
- `COMPILER_OUTPUT_LIMIT_BYTES`: max captured bytes per stream returned by this API, default `65536`
- `COMPILER_SOURCE_LIMIT_BYTES`: max execution source bytes, default `131072`

When `PISTON_BASE_URL` is empty, `/api/compiler/execute` returns `503`.
The API intentionally does not execute untrusted user code inside the FastAPI
process.

Rate limiting returns HTTP `429` with `{ "detail": "Too many runs, try again in a minute." }`.

## Persistence

Projects and shares use:

- `coding_projects`
- `coding_shares`
- `coding_executions`

`database.create_all()` will create these tables at startup. For explicit
database deployment, apply `migrations/002_coding_platform.sql`.
