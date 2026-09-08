# Coding Platform Backend API Handoff

Base URL:

```text
https://webneststudiobackend-n00h.onrender.com
```

Authenticated APIs use the existing bearer-token auth:

```http
Authorization: Bearer <accessToken>
```

General errors:

```json
{ "detail": "Human readable message." }
```

Validation errors:

```json
{
  "detail": "Request validation failed",
  "errors": [
    {
      "loc": ["body", "field"],
      "msg": "..."
    }
  ]
}
```

## Production Safety Notes

`POST /api/compiler/execute` proxies to JDoodle's hosted sandbox. The FastAPI backend intentionally does not execute untrusted user code inside the API process.

Required production env vars:

```text
JDOODLE_CLIENT_ID=your-client-id
JDOODLE_CLIENT_SECRET=your-client-secret
```

When either credential is empty, execute returns:

```http
503 Service Unavailable
```

```json
{
  "detail": "Compiler engine is not configured. Set JDOODLE_CLIENT_ID and JDOODLE_CLIENT_SECRET to enable execution."
}
```

JDoodle daily-quota exhaustion returns HTTP `429`
(`{ "detail": "Daily execution quota reached. Try again tomorrow." }`).

Runtime safety behavior:

- Compile errors and runtime errors return HTTP `200` with `status`.
- Rate limits return HTTP `429`.
- Oversized code payloads return HTTP `413`.
- Bad request shape or unknown language returns HTTP `422`.
- Compiler engine outage or invalid engine response returns HTTP `503`.
- Raw runtime exceptions are caught by the global error handler and returned as JSON.

## 1. List Compiler Languages

```http
GET /api/compiler/languages
```

Auth: public

Response `200`:

```json
[
  {
    "id": "javascript",
    "label": "JavaScript (Node)",
    "version": "20.11.1",
    "monacoId": "javascript",
    "fileExtension": "js",
    "defaultSnippet": "console.log(\"Hello, World!\");"
  },
  {
    "id": "python",
    "label": "Python 3",
    "version": "3.12.0",
    "monacoId": "python",
    "fileExtension": "py",
    "defaultSnippet": "print(\"Hello, World!\")"
  },
  {
    "id": "java",
    "label": "Java",
    "version": "21.0.2",
    "monacoId": "java",
    "fileExtension": "java",
    "defaultSnippet": "public class Main {\n  public static void main(String[] args) {\n    System.out.println(\"Hello, World!\");\n  }\n}"
  }
]
```

The implemented initial language set is:

```text
javascript, python, java, c, cpp, typescript, go, ruby
```

## 2. Execute Code

```http
POST /api/compiler/execute
Content-Type: application/json
```

Auth: public, with higher rate limit when a valid bearer token is supplied

Request with `source` alias:

```json
{
  "language": "python",
  "source": "print(input())",
  "stdin": "hello\n",
  "args": []
}
```

Request with explicit files:

```json
{
  "language": "javascript",
  "version": "20.11.1",
  "files": [
    {
      "name": "main.js",
      "content": "console.log('hello');"
    }
  ],
  "stdin": "",
  "args": []
}
```

Success response `200`:

```json
{
  "status": "success",
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "signal": null,
  "compile": null,
  "time_ms": 0,
  "wall_time_ms": 140,
  "truncated": false
}
```

Compile error response `200`:

```json
{
  "status": "compile_error",
  "stdout": "",
  "stderr": "",
  "exit_code": null,
  "signal": null,
  "compile": {
    "stdout": "",
    "stderr": "Main.java:3: error: ';' expected",
    "exit_code": 1
  },
  "time_ms": 0,
  "wall_time_ms": 200,
  "truncated": false
}
```

Runtime error response `200`:

```json
{
  "status": "runtime_error",
  "stdout": "",
  "stderr": "Traceback ...",
  "exit_code": 1,
  "signal": null,
  "compile": null,
  "time_ms": 0,
  "wall_time_ms": 120,
  "truncated": false
}
```

Rate limit response `429`:

```json
{
  "detail": "Too many runs, try again in a minute."
}
```

Unknown language response `422`:

```json
{
  "detail": "Unknown language."
}
```

Oversized source response `413`:

```json
{
  "detail": "Submitted code is too large."
}
```

## 3. List Projects

```http
GET /api/projects?limit=20&cursor=
Authorization: Bearer <accessToken>
```

Auth: required

Response `200`:

```json
{
  "items": [
    {
      "id": "9e2c0c1a-1111-4444-9999-2c1f8f67abcd",
      "title": "Fibonacci",
      "language": "python",
      "updated_at": "2026-09-08T17:20:00Z",
      "created_at": "2026-09-08T17:20:00Z"
    }
  ],
  "next_cursor": null
}
```

## 4. Create Project

```http
POST /api/projects
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Auth: required

Request:

```json
{
  "title": "Fibonacci",
  "language": "python",
  "source": "print('hello')",
  "stdin": ""
}
```

Response `201`:

```json
{
  "id": "9e2c0c1a-1111-4444-9999-2c1f8f67abcd",
  "title": "Fibonacci",
  "description": "",
  "language": "python",
  "files": [
    {
      "name": "main.py",
      "content": "print('hello')"
    }
  ],
  "stdin": "",
  "is_public": false,
  "share_id": null,
  "created_at": "2026-09-08T17:20:00Z",
  "updated_at": "2026-09-08T17:20:00Z"
}
```

## 5. Get Project

```http
GET /api/projects/9e2c0c1a-1111-4444-9999-2c1f8f67abcd
Authorization: Bearer <accessToken>
```

Auth: required, owner-scoped

Response `200`:

```json
{
  "id": "9e2c0c1a-1111-4444-9999-2c1f8f67abcd",
  "title": "Fibonacci",
  "description": "",
  "language": "python",
  "files": [
    {
      "name": "main.py",
      "content": "print('hello')"
    }
  ],
  "stdin": "",
  "is_public": false,
  "share_id": null,
  "created_at": "2026-09-08T17:20:00Z",
  "updated_at": "2026-09-08T17:20:00Z"
}
```

Not found or not owned response `404`:

```json
{
  "detail": "Project not found."
}
```

## 6. Update Project

```http
PUT /api/projects/9e2c0c1a-1111-4444-9999-2c1f8f67abcd
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Auth: required, owner-scoped

Partial request:

```json
{
  "files": [
    {
      "name": "main.py",
      "content": "print('autosaved')"
    }
  ],
  "stdin": ""
}
```

Response `200`: full updated project object.

## 7. Delete Project

```http
DELETE /api/projects/9e2c0c1a-1111-4444-9999-2c1f8f67abcd
Authorization: Bearer <accessToken>
```

Auth: required, owner-scoped

Response:

```http
204 No Content
```

Deleting a project does not delete existing share snapshots.

## 8. Create Share

```http
POST /api/shares
Authorization: Bearer <accessToken>
Content-Type: application/json
```

Auth: required

Request:

```json
{
  "project_id": "9e2c0c1a-1111-4444-9999-2c1f8f67abcd",
  "title": "Fibonacci",
  "language": "python",
  "source": "print('hello')",
  "stdin": "",
  "stdout": "hello\n"
}
```

Response `200`:

```json
{
  "share_id": "s7Kq2mA9xYz1",
  "url": "/s/s7Kq2mA9xYz1"
}
```

## 9. Get Share

```http
GET /api/shares/s7Kq2mA9xYz1
```

Auth: public

Response `200`:

```json
{
  "share_id": "s7Kq2mA9xYz1",
  "title": "Fibonacci",
  "language": "python",
  "files": [
    {
      "name": "main.py",
      "content": "print('hello')"
    }
  ],
  "stdin": "",
  "stdout": "hello\n",
  "created_at": "2026-09-08T17:25:00Z",
  "author_display_name": "Vansh"
}
```

Unknown share response `404`:

```json
{
  "detail": "Share not found."
}
```

## 10. Coding Stats

```http
GET /api/me/coding-stats
Authorization: Bearer <accessToken>
```

Auth: required

Response `200`:

```json
{
  "projects_count": 1,
  "last_activity_at": "2026-09-08T17:20:00Z"
}
```

## Database Patch

Apply this migration in production:

```text
migrations/002_coding_platform.sql
```

It creates:

```text
coding_projects
coding_shares
coding_executions
```

The patch was already applied and verified on the currently configured database.
