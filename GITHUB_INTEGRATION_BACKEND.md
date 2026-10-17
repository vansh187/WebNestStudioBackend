# Webnest CodeLab GitHub Integration - Backend

This document covers the backend side of GitHub sync in the CodeLab playground. The frontend side is in `GITHUB_INTEGRATION_FRONTEND.md` (frontend repo). **Section 2 is identical in both documents**: change it in both or not at all.

## 1. What the backend does

- Runs the GitHub OAuth flow for a logged-in WebNest user and stores their GitHub token, encrypted.
- Proxies the GitHub REST API for the playground: list repositories and branches, create a repository, read a branch snapshot, write a commit.
- Translates every GitHub failure into the error codes of Section 2. GitHub problems never answer `401`.

## 2. API contract

Base URL: `VITE_API_BASE_URL`. **Every endpoint requires** `Authorization: Bearer <WebNest access token>`. JSON in and out. Timestamps ISO-8601 UTC.

Error body: `{ "detail": "Human readable message.", "code": "<machine_code>" }` plus the extra fields listed. Validation errors: `422 { "errors": [ { "loc": ["body", "<field>"], "msg": "..." } ] }`.

| HTTP | code | When | Extra fields |
|---|---|---|---|
| 400 | `invalid_state` | OAuth state missing, expired, already used or owned by another user | |
| 400 | `oauth_exchange_failed` | GitHub rejected the code, or the learner did not grant the `repo` scope | |
| 404 | `repo_not_found` | Repository or branch missing, or the user has no access | |
| 409 | `github_not_connected` | The user has no GitHub connection | |
| 409 | `github_reauth_required` | The stored token was revoked or expired. **Never send 401 for this**: 401 makes the frontend refresh the WebNest session and log the user out | |
| 409 | `non_fast_forward` | Branch head is not `base_commit_sha` | `remote_commit_sha` |
| 413 | `payload_too_large` | Commit has more than 300 changes or more than 2 MB of content | |
| 429 | `github_rate_limited` | GitHub rate limit reached | `retry_after` (seconds) |
| 502 | `github_unavailable` | Network error, timeout or 5xx from GitHub | |

`401` is reserved for an invalid or expired **WebNest** token (existing behaviour).

### 2.1 `GET /api/github/connection`

200:

```json
{ "connected": true, "login": "octocat", "avatar_url": "https://avatars.githubusercontent.com/u/1", "scopes": ["repo"], "connected_at": "2026-10-18T10:00:00Z" }
```

Not connected (still 200):

```json
{ "connected": false, "login": null, "avatar_url": null, "scopes": [], "connected_at": null }
```

### 2.2 `POST /api/github/oauth/start`

Request:

```json
{ "return_to": "/codelab/playground" }
```

`return_to` must start with `/codelab/` (otherwise `422`). 200:

```json
{ "authorize_url": "https://github.com/login/oauth/authorize?client_id=...&redirect_uri=...&scope=repo&state=..." }
```

`authorize_url` must start with `https://github.com/` (the frontend rejects anything else).

### 2.3 `POST /api/github/oauth/callback`

Request:

```json
{ "code": "abc123", "state": "xyz789" }
```

200:

```json
{ "connected": true, "login": "octocat", "avatar_url": "https://avatars.githubusercontent.com/u/1", "scopes": ["repo"], "connected_at": "2026-10-18T10:00:00Z", "return_to": "/codelab/playground" }
```

Errors: `400 invalid_state`, `400 oauth_exchange_failed`.

### 2.4 `DELETE /api/github/connection`

204, no body. Revokes the grant on GitHub (best effort) and deletes the stored token. Idempotent: 204 even when nothing was connected.

### 2.5 `GET /api/github/repos?page=1&per_page=30&q=`

200:

```json
{
  "items": [
    {
      "owner": "octocat",
      "name": "todo-api",
      "full_name": "octocat/todo-api",
      "private": true,
      "default_branch": "main",
      "html_url": "https://github.com/octocat/todo-api",
      "updated_at": "2026-10-17T08:00:00Z"
    }
  ],
  "page": 1,
  "has_more": false
}
```

Sorted by `updated_at` descending. `q` filters by repository name, case-insensitive (optional). `per_page` defaults to 30, max 100.

### 2.6 `GET /api/github/repos/{owner}/{repo}/branches`

200:

```json
{ "items": [ { "name": "main", "commit_sha": "4f2c..." } ] }
```

### 2.7 `POST /api/github/repos`

Request:

```json
{ "name": "todo-api", "private": true, "description": "Made with WebNest CodeLab" }
```

`name`: 1-100 characters from `[A-Za-z0-9._-]`. The repository is created with `auto_init: true`, so it always has a first commit. 201:

```json
{
  "owner": "octocat",
  "name": "todo-api",
  "full_name": "octocat/todo-api",
  "private": true,
  "default_branch": "main",
  "html_url": "https://github.com/octocat/todo-api",
  "head_commit_sha": "9a1b..."
}
```

Name already taken:

```json
{ "errors": [ { "loc": ["body", "name"], "msg": "A repository with this name already exists." } ] }
```

with status `422`.

### 2.8 `GET /api/github/repos/{owner}/{repo}/snapshot?branch=main&mode=auto`

`branch` is optional (default branch when omitted). `mode` is one of `auto|backend|web|python|java`. It only decides the returned `mode` value: with `auto` the backend guesses it, otherwise it echoes the value. The backend does not filter files by mode. 200:

```json
{
  "owner": "octocat",
  "repo": "todo-api",
  "branch": "main",
  "commit_sha": "4f2c...",
  "mode": "backend",
  "files": [
    { "path": "app/main.py", "language": "python", "content": "from fastapi import FastAPI\n", "sha": "e69d..." }
  ],
  "skipped": [ { "path": "logo.png", "reason": "binary" } ]
}
```

`sha` is the **git blob SHA** returned by GitHub, unchanged; the frontend uses it to detect changes. `reason` is one of `binary|too_large|ignored_dir|limit`. Limits: 200 KB per file, 300 files, 2 MB total.

### 2.9 `POST /api/github/repos/{owner}/{repo}/commits`

Request:

```json
{
  "branch": "main",
  "base_commit_sha": "4f2c...",
  "message": "Update task service",
  "changes": [
    { "path": "app/services/task_service.py", "action": "upsert", "content": "..." },
    { "path": "old.py", "action": "delete" }
  ]
}
```

- `action` is `upsert` or `delete`.
- `content` is required for `upsert` and must be absent for `delete`.
- `message` is 1-200 characters.
- `changes` has 1-300 items.
- Paths are relative, use `/`, and contain no `..` or leading `/`.

201:

```json
{
  "commit_sha": "7c3e...",
  "html_url": "https://github.com/octocat/todo-api/commit/7c3e...",
  "files": [ { "path": "app/services/task_service.py", "sha": "b1d0..." } ]
}
```

`files` lists the new blob SHA of every upserted path. Deleted paths are left out. If the branch head is not `base_commit_sha`, the response is `409 non_fast_forward` with `remote_commit_sha`, and nothing is written.

## 3. Code map

| File | Role |
|---|---|
| `api/github_router.py` | The Section 2 routes, all behind `get_current_user` |
| `services/github_service.py` | OAuth, repositories, snapshot planning, commits, token encryption |
| `services/github_client.py` | GitHub HTTP client and the mapping of GitHub responses to Section 2 errors |
| `database/github_persistence.py` | Connection and OAuth-state storage |
| `schemas/github_schemas.py` | Request/response models and input validation (paths, branch, `return_to`, repo name) |
| `core/request_limits.py` | Rejects `/api/github/*` request bodies over 16 MB before they are read |
| `core/error_handlers.py` | Renders `GitHubError` as `{detail, code, ...extra}`; validation errors on `/api/github/*` as `{loc, msg}` only |
| `migrations/011_github_integration.sql` | `github_connections`, `github_oauth_states` |
| `tests/test_github_integration.py` | Contract errors, validation, snapshot limits, commit flow, OAuth state, response shapes |

## 4. Behaviour notes

- **Commits** use the Git Data API: check the branch head against `base_commit_sha`, build a tree on top of the head's tree, create the commit, then move the branch with `force: false`. If someone pushes in between, the ref update fails and the response is `409 non_fast_forward` with the new head. If the head did not move but GitHub still refuses the update (branch protection), the response is `422` with a message saying the branch may be protected. The branch is never rewritten.
- An existing file's executable bit (`100755`) is kept on upsert. Deleting a path that does not exist is ignored. A commit whose tree is unchanged returns the current head without creating an empty commit. When GitHub truncates the base tree listing (very large repositories), the changed paths are looked up directory by directory, so both rules still hold.
- `files[].sha` in the commit response is computed as git does (`sha1("blob <len>\0" + utf-8 bytes)`), so it equals what GitHub stores.
- **Snapshot**: files are taken in path order until a limit is reached. An ignored directory (`node_modules/`, `.git/`, `__pycache__/`, `dist/`, `build/`, `target/`, `.venv/`, ...) is reported once as `{ "path": "node_modules/", "reason": "ignored_dir" }`, not once per file. Symlinks, submodules, known binary extensions and any content with a NUL byte or invalid UTF-8 are `binary`. At most 1000 `skipped` entries are listed. If GitHub truncates the recursive tree, the backend lists directories one by one, up to 150 of them. Directories it did not reach are reported as `{ "path": "<dir>/", "reason": "limit" }`, so a partial import never looks complete. The first failed blob download cancels the rest.
- **Mode guessing:** a Python project using FastAPI/Flask/Django/Starlette is `backend` however many HTML/JS/CSS files it has.
- **Connection problems:** GitHub reads (GET/DELETE) are retried twice (after 0.5 s and 1.5 s) on connection errors, timeouts and 502/503/504. Writes are retried only when the request never reached GitHub (connect failure), so a commit or repository is never created twice. What still fails is `502 github_unavailable`.
- **Error boundary:** every service method ends in a catch-all. An unexpected error is logged server-side and answered as `500 { "detail": "Something went wrong with GitHub sync. Please try again." }`, with no internal detail.
- **`mode=auto`**: `java` when Java files dominate; `backend` for Python using FastAPI/Flask/Django/Starlette (imports or `requirements.txt`), or Node with Express/Fastify/Koa/Nest; `python` for other Python; otherwise `web`.
- **Repo search (`q`)**: GitHub can't filter `/user/repos` by name, so the backend scans the most recently updated repositories (at most 1000) and paginates the matches itself. It stops as soon as the requested page, plus one more match for `has_more`, is filled.
- **GitHub 403** without a rate-limit signal and **409** (empty repository) are reported as `404 repo_not_found`. A GitHub `401` becomes `409 github_reauth_required`, and the stored token is deleted, so `GET /connection` then reads as disconnected.

## 5. Security and data protection

- **Token at rest**: the GitHub token is Fernet-encrypted (AES-128-CBC + HMAC-SHA256) in `github_connections.access_token_encrypted`. It is decrypted only in memory for the GitHub calls of one request. It is never returned by any endpoint and never logged.
- **OAuth CSRF**: `state` is 256 bits from `secrets`. Only its SHA-256 is stored. It is bound to the WebNest user who started the flow, valid for 10 minutes, and consumed by one atomic `UPDATE ... WHERE used_at IS NULL`. Up to 5 flows per user can be open at once (double click, two tabs). Starting a sixth drops the oldest.
- **Open redirect**: `return_to` must be a same-site path under `/codelab/`. Schemes, hosts, `//`, `\` and `..` are rejected. `authorize_url` is always built by the backend on `https://github.com/`, with the configured `redirect_uri`.
- **Request forgery towards GitHub**: requests go only to `https://api.github.com` and `https://github.com`, with redirects disabled. `owner`, `repo` and `branch` are validated (GitHub naming and git ref-name rules) and URL-encoded before they become part of a GitHub URL. Invalid values get `404` without any GitHub call.
- **Commit paths**: relative, `/`-separated, no empty, `.` or `..` segments, nothing in `.git/`, no control characters, no duplicates.
- **Resource limits**: 16 MB request body cap on `/api/github/*` (enforced before parsing), 300 changes and 2 MB of content per commit, 300 files / 2 MB / 200 KB per file per snapshot, at most 8 parallel blob downloads, 20 s timeout per GitHub call. Database connections are released before GitHub calls start.
- **No data leaks in errors**: GitHub's error messages, which can name private repositories, are not passed through. Validation errors on `/api/github/*` carry only `loc` and `msg`, never the submitted input (which would be file contents).
- **Least privilege**: the OAuth scope is `repo` because learners import and push private repositories. GitHub has no narrower classic-OAuth scope for that. The token can therefore read and write every repository the learner can access: say so on the Connect GitHub screen. A GitHub App with per-repository installation would be narrower; that is a possible later change.
- **Retention and erasure**: Disconnect deletes the token locally first, then revokes the grant on GitHub. Reconnecting revokes the previous token, and only that token, because the new one can belong to the same grant. A misconfigured encryption key answers `502` and never deletes stored connections. Deleting a WebNest user cascades to both tables. OAuth states are deleted when the user starts a new flow, and expired states are purged a day after expiry. File contents pass through the backend and are never stored.

## 6. Configuration

Create a GitHub OAuth App (GitHub → Settings → Developer settings → OAuth Apps), with **Authorization callback URL** `https://www.webneststudio.co.in/codelab/github/callback`.

| Env var | Required | Notes |
|---|---|---|
| `GITHUB_CLIENT_ID` | yes | OAuth App client id |
| `GITHUB_CLIENT_SECRET` | yes | OAuth App secret. Never commit it |
| `GITHUB_TOKEN_ENCRYPTION_KEY` | recommended | Output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. When blank, a key is derived from `JWT_SECRET_KEY`, and rotating that secret then asks every learner to reconnect |
| `GITHUB_OAUTH_REDIRECT_URI` | no | Defaults to `<FRONTEND_BASE_URL>/codelab/github/callback`. Must match the OAuth App exactly |
| `GITHUB_API_TIMEOUT_SECONDS` | no | Default `20` |

Without the client id and secret, `/oauth/start` and `/oauth/callback` answer `502 github_unavailable`. The other routes still answer `409 github_not_connected`.

The tables are created at startup (`create_all`). `migrations/011_github_integration.sql` does the same by hand and is safe to re-run.

## 7. Testing

`python -m pytest tests/test_github_integration.py` runs against an in-memory fake of the GitHub API (no network, no database). It covers:

- blob SHAs against `git hash-object`
- the commit flow: executable bit kept, missing deletes ignored, stale base, losing the race on the ref update, and the 413 limits
- every error mapping
- input validation: paths, branch, repo name and `return_to`
- OAuth state: single use, user-bound, hashed
- token encryption and key rotation
- snapshot limits and mode guessing
- the exact JSON shapes of error and validation responses, and the body-size middleware
