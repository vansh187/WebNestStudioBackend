# CodeLab + Learning API

Browser-first coding practice and study material APIs.

- Base URL: `https://webneststudiobackend-n00h.onrender.com`
- Public reads accept no token; if a bearer token is present, per-user progress is included.
- User state writes require `Authorization: Bearer <access_token>`.
- Admin writes require an admin bearer token.
- All ids are UUID strings unless a route explicitly asks for a slug.
- The backend does not execute learner code for `/api/codelab`; clients run code in Pyodide or an iframe and submit result summaries for persistence, scoring, XP, and streaks.

## CodeLab

### `GET /api/codelab/tracks`

Returns the published practice tracks.

```json
{
  "items": [
    {
      "id": "python",
      "label": "Python",
      "runner": "pyodide",
      "description": "Python practice with browser-side execution."
    }
  ]
}
```

### `GET /api/codelab/problems`

Query params:

- `track`: optional track id, for example `python`
- `language`: optional language id
- `topic`: optional exact topic
- `difficulty`: `easy` | `medium` | `hard`
- `limit`: `1` to `100`, default `20`
- `cursor`: opaque pagination cursor from the previous response

Returns published problems with `status` set to the current user's progress state when authenticated.

### `GET /api/codelab/problems/{identifier}`

`identifier` may be a problem slug or UUID. Returns the problem statement, starter files, examples, public tests, and optional user progress. Hidden tests are never returned.

### `POST /api/codelab/submissions`

Requires auth. Stores a client-reported execution result and updates progress, XP, streaks, and first-solve achievement.

```json
{
  "problem_id": "hello-python",
  "language": "python",
  "files": [
    {
      "name": "main.py",
      "language": "python",
      "content": "print('hello')"
    }
  ],
  "result": {
    "status": "passed",
    "score": 100,
    "passed_tests": 3,
    "total_tests": 3,
    "stdout": "hello\n",
    "stderr": "",
    "runtime_ms": 42
  }
}
```

`result.status` is one of `passed`, `failed`, `runtime_error`, `timeout`, or `manual_review`.

### `GET /api/codelab/submissions`

Requires auth. Optional query params:

- `problem_id`: problem slug or UUID
- `limit`: `1` to `100`, default `20`
- `cursor`: opaque pagination cursor

### `GET /api/codelab/dashboard`

Requires auth. Returns totals, solved counts, XP, streaks, per-track progress, per-difficulty progress, recent submissions, achievements, and the next problem to continue.

## CodeLab Admin

### `PUT /api/admin/codelab/problems`

Creates or replaces a problem keyed by `slug`, including its complete test-case set.

```json
{
  "title": "Hello Python",
  "slug": "hello-python",
  "track": "python",
  "language": "python",
  "difficulty": "easy",
  "points": 20,
  "estimated_minutes": 5,
  "status": "published",
  "topics": ["basics", "stdout"],
  "statement": "Print hello.",
  "constraints": [],
  "hints": ["Use print()."],
  "starter_files": [
    { "name": "main.py", "language": "python", "content": "" }
  ],
  "examples": [
    { "input": "", "expected_output": "hello", "explanation": null }
  ],
  "test_cases": [
    {
      "input": "",
      "expected_output": "hello",
      "is_hidden": false,
      "weight": 1
    }
  ]
}
```

## Learning

### `GET /api/learning/courses`

Returns published courses with lesson counts and optional authenticated user completion percentages.

### `GET /api/learning/courses/{slug}`

Returns a published course outline with modules and published lessons. Lesson statuses reflect the current user when authenticated.

### `GET /api/learning/lessons/{lesson_id}`

Returns lesson content, resources, linked practice problems, and optional authenticated user progress/bookmark/note state.

### `PUT /api/learning/lessons/{lesson_id}/progress`

Requires auth.

```json
{
  "status": "completed",
  "completed_percent": 100,
  "time_spent_seconds": 360
}
```

Completing a lesson rolls the shared learning streak.

### `PUT /api/learning/lessons/{lesson_id}/bookmark`

Requires auth.

```json
{ "bookmarked": true }
```

### `PUT /api/learning/lessons/{lesson_id}/note`

Requires auth.

```json
{ "note": "Remember to revisit list slicing." }
```

### `GET /api/learning/quizzes/{quiz_id}`

Returns quiz questions without correct answers. Includes `already_passed` when authenticated.

### `POST /api/learning/quizzes/{quiz_id}/submit`

Requires auth.

```json
{
  "answers": [
    { "question_id": "a1b2c3d4-0000-0000-0000-000000000000", "answer": "option-a" }
  ]
}
```

Passing a quiz awards XP once, then only records additional attempts.

### `GET /api/learning/dashboard`

Requires auth. Returns course progress, completed lessons, solved CodeLab problems, passed quizzes, XP, streak, recent learning activity, and a lesson to continue.

## Learning Admin

### `PUT /api/admin/learning/courses`

Creates or updates a whole course tree keyed by `slug`. Existing modules and lessons are matched by `id` when provided. `practice_problem_slugs` must reference existing CodeLab problem slugs.

```json
{
  "title": "Python Foundations",
  "slug": "python-foundations",
  "level": "beginner",
  "description": "Core Python lessons with practice.",
  "status": "published",
  "display_order": 1,
  "modules": [
    {
      "title": "Getting Started",
      "display_order": 1,
      "status": "published",
      "lessons": [
        {
          "title": "Printing Output",
          "content": {
            "format": "html",
            "body": "<p>Use <code>print()</code> to write output.</p>"
          },
          "resources": [],
          "estimated_minutes": 8,
          "status": "published",
          "display_order": 1,
          "practice_problem_slugs": ["hello-python"]
        }
      ]
    }
  ]
}
```

### `GET /api/admin/learning/courses/{slug}`

Returns the full admin course tree, including unpublished modules/lessons and practice problem slugs.

## Persistence

Apply `migrations/004_codelab_learning.sql` for controlled database deployments. The app also calls `Base.metadata.create_all()` at startup.

Tables added:

- `codelab_tracks`, `codelab_problems`, `codelab_test_cases`
- `codelab_submissions`, `codelab_problem_progress`, `codelab_user_stats`, `codelab_achievements`
- `courses`, `course_modules`, `lessons`, `lesson_problem_map`
- `quizzes`, `quiz_questions`, `quiz_attempts`
- `user_course_progress`, `user_lesson_progress`, `user_lesson_bookmarks`, `user_lesson_notes`
