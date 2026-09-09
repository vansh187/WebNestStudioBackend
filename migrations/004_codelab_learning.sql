-- =========================================================================== --
-- Webnest CodeLab + Learning platform (Phase 1).
--
-- Adds the browser-first coding-practice tables (tracks, problems, test cases,
-- submissions, per-user progress / stats / achievements) and the study-material
-- tables (courses, modules, lessons, quizzes, per-user progress / bookmarks /
-- notes). The backend never executes visitor code; submissions carry a
-- client-reported result blob that is stored for audit only.
--
-- Idempotent: safe to run more than once. The application also builds these
-- tables from the ORM metadata at startup (Base.metadata.create_all); apply this
-- file directly for a controlled/Supabase database deployment.
-- =========================================================================== --

-- --------------------------------------------------------------------------- --
-- CodeLab: tracks & problems
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS codelab_tracks (
    id            text PRIMARY KEY,
    label         text NOT NULL,
    runner        text NOT NULL DEFAULT 'iframe',      -- pyodide | iframe
    description   text NOT NULL DEFAULT '',
    display_order integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'published',   -- draft | published
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);

INSERT INTO codelab_tracks (id, label, runner, description, display_order)
VALUES
    ('python', 'Python', 'pyodide', 'Python practice with browser-side execution.', 1),
    ('web', 'Web Development', 'iframe', 'HTML, CSS, and JavaScript challenges.', 2)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS codelab_problems (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug              text NOT NULL UNIQUE,
    title             text NOT NULL,
    track             text NOT NULL,
    language          text NOT NULL,
    difficulty        text NOT NULL DEFAULT 'easy',
    statement         text NOT NULL DEFAULT '',
    constraints       jsonb,
    hints             jsonb,
    starter_files     jsonb,
    examples          jsonb,
    topics            text[],
    points            integer NOT NULL DEFAULT 20,
    estimated_minutes integer NOT NULL DEFAULT 10,
    status            text NOT NULL DEFAULT 'draft',
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now(),
    CONSTRAINT ck_codelab_problem_difficulty CHECK (difficulty IN ('easy', 'medium', 'hard')),
    CONSTRAINT ck_codelab_problem_status CHECK (status IN ('draft', 'published', 'archived'))
);

CREATE INDEX IF NOT EXISTS ix_codelab_problems_slug ON codelab_problems (slug);
CREATE INDEX IF NOT EXISTS ix_codelab_problems_track ON codelab_problems (track);
CREATE INDEX IF NOT EXISTS ix_codelab_problems_status ON codelab_problems (status);
CREATE INDEX IF NOT EXISTS ix_codelab_problems_track_status ON codelab_problems (track, status);

CREATE TABLE IF NOT EXISTS codelab_test_cases (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    problem_id      uuid NOT NULL REFERENCES codelab_problems(id) ON DELETE CASCADE,
    input           text NOT NULL DEFAULT '',
    expected_output text NOT NULL DEFAULT '',
    is_hidden       boolean NOT NULL DEFAULT false,
    weight          integer NOT NULL DEFAULT 1,
    display_order   integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_codelab_test_cases_problem_id ON codelab_test_cases (problem_id);

-- --------------------------------------------------------------------------- --
-- CodeLab: submissions & per-user state
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS codelab_submissions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    problem_id   uuid NOT NULL REFERENCES codelab_problems(id) ON DELETE CASCADE,
    language     text NOT NULL,
    files        jsonb,
    result       jsonb,
    status       text NOT NULL,   -- passed | failed | runtime_error | timeout | manual_review
    score        integer NOT NULL DEFAULT 0,
    passed_tests integer NOT NULL DEFAULT 0,
    total_tests  integer NOT NULL DEFAULT 0,
    runtime_ms   integer,
    submitted_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_codelab_submissions_user_id ON codelab_submissions (user_id);
CREATE INDEX IF NOT EXISTS ix_codelab_submissions_problem_id ON codelab_submissions (problem_id);
CREATE INDEX IF NOT EXISTS ix_codelab_submissions_submitted_at ON codelab_submissions (submitted_at);
CREATE INDEX IF NOT EXISTS ix_codelab_submissions_user_created ON codelab_submissions (user_id, submitted_at);

CREATE TABLE IF NOT EXISTS codelab_problem_progress (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    problem_id       uuid NOT NULL REFERENCES codelab_problems(id) ON DELETE CASCADE,
    status           text NOT NULL DEFAULT 'not_started',   -- not_started | in_progress | solved
    best_score       integer NOT NULL DEFAULT 0,
    attempts         integer NOT NULL DEFAULT 0,
    solved_at        timestamptz,
    last_activity_at timestamptz DEFAULT now(),
    CONSTRAINT uq_codelab_problem_progress UNIQUE (user_id, problem_id)
);

CREATE INDEX IF NOT EXISTS ix_codelab_problem_progress_user_id ON codelab_problem_progress (user_id);
CREATE INDEX IF NOT EXISTS ix_codelab_problem_progress_problem_id ON codelab_problem_progress (problem_id);

CREATE TABLE IF NOT EXISTS codelab_user_stats (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    xp                 integer NOT NULL DEFAULT 0,
    current_streak     integer NOT NULL DEFAULT 0,
    best_streak        integer NOT NULL DEFAULT 0,
    solved_count       integer NOT NULL DEFAULT 0,
    last_activity_date date,
    updated_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_codelab_user_stats_user_id ON codelab_user_stats (user_id);

CREATE TABLE IF NOT EXISTS codelab_achievements (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    achievement_key text NOT NULL,
    label           text NOT NULL,
    earned_at       timestamptz DEFAULT now(),
    CONSTRAINT uq_codelab_achievement UNIQUE (user_id, achievement_key)
);

CREATE INDEX IF NOT EXISTS ix_codelab_achievements_user_id ON codelab_achievements (user_id);

-- --------------------------------------------------------------------------- --
-- Learning: courses / modules / lessons
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS courses (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          text NOT NULL UNIQUE,
    title         text NOT NULL,
    level         text NOT NULL DEFAULT 'beginner',
    description   text NOT NULL DEFAULT '',
    status        text NOT NULL DEFAULT 'draft',
    display_order integer NOT NULL DEFAULT 0,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now(),
    CONSTRAINT ck_course_status CHECK (status IN ('draft', 'published', 'archived'))
);

CREATE INDEX IF NOT EXISTS ix_courses_slug ON courses (slug);
CREATE INDEX IF NOT EXISTS ix_courses_status ON courses (status);

CREATE TABLE IF NOT EXISTS course_modules (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    course_id     uuid NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title         text NOT NULL,
    display_order integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'published',
    created_at    timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_course_modules_course_id ON course_modules (course_id);

CREATE TABLE IF NOT EXISTS lessons (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id         uuid NOT NULL REFERENCES course_modules(id) ON DELETE CASCADE,
    course_id         uuid NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title             text NOT NULL,
    content           jsonb,
    resources         jsonb,
    estimated_minutes integer NOT NULL DEFAULT 8,
    status            text NOT NULL DEFAULT 'draft',
    display_order     integer NOT NULL DEFAULT 0,
    created_at        timestamptz DEFAULT now(),
    updated_at        timestamptz DEFAULT now(),
    CONSTRAINT ck_lesson_status CHECK (status IN ('draft', 'published', 'archived'))
);

CREATE INDEX IF NOT EXISTS ix_lessons_module_id ON lessons (module_id);
CREATE INDEX IF NOT EXISTS ix_lessons_course_id ON lessons (course_id);
CREATE INDEX IF NOT EXISTS ix_lessons_status ON lessons (status);

CREATE TABLE IF NOT EXISTS lesson_problem_map (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_id  uuid NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    problem_id uuid NOT NULL REFERENCES codelab_problems(id) ON DELETE CASCADE,
    CONSTRAINT uq_lesson_problem_map UNIQUE (lesson_id, problem_id)
);

CREATE INDEX IF NOT EXISTS ix_lesson_problem_map_lesson_id ON lesson_problem_map (lesson_id);
CREATE INDEX IF NOT EXISTS ix_lesson_problem_map_problem_id ON lesson_problem_map (problem_id);

-- --------------------------------------------------------------------------- --
-- Learning: quizzes
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS quizzes (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_id    uuid REFERENCES lessons(id) ON DELETE CASCADE,
    title        text NOT NULL DEFAULT 'Quiz',
    pass_percent integer NOT NULL DEFAULT 70,
    xp_reward    integer NOT NULL DEFAULT 10,
    status       text NOT NULL DEFAULT 'draft',
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_quizzes_lesson_id ON quizzes (lesson_id);
CREATE INDEX IF NOT EXISTS ix_quizzes_status ON quizzes (status);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    quiz_id        uuid NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    prompt         text NOT NULL,
    kind           text NOT NULL DEFAULT 'single',   -- single | boolean
    options        jsonb,
    correct_answer jsonb,
    explanation    text NOT NULL DEFAULT '',
    display_order  integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_quiz_questions_quiz_id ON quiz_questions (quiz_id);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quiz_id      uuid NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    score        integer NOT NULL DEFAULT 0,
    total        integer NOT NULL DEFAULT 0,
    passed       boolean NOT NULL DEFAULT false,
    answers      jsonb,
    submitted_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_quiz_attempts_user_id ON quiz_attempts (user_id);
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_quiz_id ON quiz_attempts (quiz_id);
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_submitted_at ON quiz_attempts (submitted_at);
CREATE INDEX IF NOT EXISTS ix_quiz_attempts_user_quiz ON quiz_attempts (user_id, quiz_id);

-- --------------------------------------------------------------------------- --
-- Learning: per-user progress / bookmarks / notes
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS user_course_progress (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id          uuid NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    completion_percent integer NOT NULL DEFAULT 0,
    updated_at         timestamptz DEFAULT now(),
    CONSTRAINT uq_user_course_progress UNIQUE (user_id, course_id)
);

CREATE INDEX IF NOT EXISTS ix_user_course_progress_user_id ON user_course_progress (user_id);
CREATE INDEX IF NOT EXISTS ix_user_course_progress_course_id ON user_course_progress (course_id);

CREATE TABLE IF NOT EXISTS user_lesson_progress (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_id          uuid NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    status             text NOT NULL DEFAULT 'not_started',   -- not_started | in_progress | completed
    completed_percent  integer NOT NULL DEFAULT 0,
    time_spent_seconds integer NOT NULL DEFAULT 0,
    updated_at         timestamptz DEFAULT now(),
    CONSTRAINT uq_user_lesson_progress UNIQUE (user_id, lesson_id)
);

CREATE INDEX IF NOT EXISTS ix_user_lesson_progress_user_id ON user_lesson_progress (user_id);
CREATE INDEX IF NOT EXISTS ix_user_lesson_progress_lesson_id ON user_lesson_progress (lesson_id);

CREATE TABLE IF NOT EXISTS user_lesson_bookmarks (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_id  uuid NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    bookmarked boolean NOT NULL DEFAULT true,
    updated_at timestamptz DEFAULT now(),
    CONSTRAINT uq_user_lesson_bookmark UNIQUE (user_id, lesson_id)
);

CREATE INDEX IF NOT EXISTS ix_user_lesson_bookmarks_user_id ON user_lesson_bookmarks (user_id);
CREATE INDEX IF NOT EXISTS ix_user_lesson_bookmarks_lesson_id ON user_lesson_bookmarks (lesson_id);

CREATE TABLE IF NOT EXISTS user_lesson_notes (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_id  uuid NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    note       text NOT NULL DEFAULT '',
    updated_at timestamptz DEFAULT now(),
    CONSTRAINT uq_user_lesson_note UNIQUE (user_id, lesson_id)
);

CREATE INDEX IF NOT EXISTS ix_user_lesson_notes_user_id ON user_lesson_notes (user_id);
CREATE INDEX IF NOT EXISTS ix_user_lesson_notes_lesson_id ON user_lesson_notes (lesson_id);
