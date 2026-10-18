-- =========================================================================== --
-- Course final exams and certificates (COURSE_EXAMS_CERTIFICATES.md,
-- COURSE_EXAMS_BACKEND_PLAN.md section 3).
--
-- The application applies all of this itself at startup: exams/schema_upgrade.py
-- adds the columns/keys to existing tables (before create_all), and create_all
-- builds the exam tables. Running this file by hand is optional and safe to
-- repeat. Generated from the ORM models - keep them in sync.
-- =========================================================================== --

-- Existing learning tables
ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_code text;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE courses ADD COLUMN IF NOT EXISTS exam_ready_at timestamptz;
ALTER TABLE course_modules ADD COLUMN IF NOT EXISTS key text;
ALTER TABLE lessons ADD COLUMN IF NOT EXISTS key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_courses_exam_code ON courses (exam_code);
CREATE UNIQUE INDEX IF NOT EXISTS uq_course_modules_course_id_id ON course_modules (course_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_course_modules_course_key ON course_modules (course_id, key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lessons_module_id_id ON lessons (module_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lessons_course_key ON lessons (course_id, key);
CREATE INDEX IF NOT EXISTS ix_lessons_key ON lessons (key);

-- exam_generation_jobs
CREATE TABLE IF NOT EXISTS exam_generation_jobs (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	course_id UUID NOT NULL, 
	trigger TEXT NOT NULL, 
	status TEXT NOT NULL, 
	progress JSONB DEFAULT '{}'::jsonb NOT NULL, 
	calls INTEGER DEFAULT '0' NOT NULL, 
	created INTEGER DEFAULT '0' NOT NULL, 
	rejected JSONB DEFAULT '{}'::jsonb NOT NULL, 
	errors JSONB DEFAULT '[]'::jsonb NOT NULL, 
	paused_until TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE, 
	heartbeat_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_exam_generation_jobs_status CHECK (status in ('running','done','failed','paused')), 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_exam_generation_jobs_course_created ON exam_generation_jobs (course_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_generation_jobs_course_running ON exam_generation_jobs (course_id) WHERE status = 'running';

-- exam_questions
CREATE TABLE IF NOT EXISTS exam_questions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	course_id UUID NOT NULL, 
	module_id UUID NOT NULL, 
	lesson_id UUID NOT NULL, 
	concept TEXT NOT NULL, 
	kind TEXT DEFAULT 'concept' NOT NULL, 
	difficulty TEXT NOT NULL, 
	prompt TEXT NOT NULL, 
	options JSONB NOT NULL, 
	correct_index INTEGER NOT NULL, 
	explanation TEXT DEFAULT '' NOT NULL, 
	why_others_wrong JSONB, 
	source_fact TEXT, 
	code TEXT, 
	code_language TEXT, 
	expected_output TEXT, 
	status TEXT NOT NULL, 
	source TEXT DEFAULT 'ai' NOT NULL, 
	reject_reason TEXT, 
	prompt_norm TEXT NOT NULL, 
	prompt_hash TEXT NOT NULL, 
	embedding FLOAT[], 
	checks JSONB, 
	generator_model TEXT, 
	generation_job_id UUID, 
	times_served INTEGER DEFAULT '0' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_exam_questions_options CHECK (jsonb_typeof(options) = 'array' and jsonb_array_length(options) = 4), 
	CONSTRAINT ck_exam_questions_correct_index CHECK (correct_index between 0 and 3), 
	CONSTRAINT ck_exam_questions_status CHECK (status in ('live','rejected','quarantined','voided','retired')), 
	CONSTRAINT ck_exam_questions_difficulty CHECK (difficulty in ('easy','medium','hard')), 
	CONSTRAINT ck_exam_questions_kind CHECK (kind in ('concept','scenario','code_output','spot_the_bug')), 
	CONSTRAINT ck_exam_questions_source CHECK (source in ('ai','manual','seed')), 
	CONSTRAINT fk_exam_questions_course_module FOREIGN KEY(course_id, module_id) REFERENCES course_modules (course_id, id) ON DELETE CASCADE ON UPDATE CASCADE, 
	CONSTRAINT fk_exam_questions_module_lesson FOREIGN KEY(module_id, lesson_id) REFERENCES lessons (module_id, id) ON DELETE CASCADE ON UPDATE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(generation_job_id) REFERENCES exam_generation_jobs (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_exam_questions_course_lesson_concept ON exam_questions (course_id, lesson_id, concept);
CREATE INDEX IF NOT EXISTS ix_exam_questions_course_status_module ON exam_questions (course_id, status, module_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_questions_course_hash ON exam_questions (course_id, prompt_hash) WHERE status <> 'rejected';

-- exam_module_context
CREATE TABLE IF NOT EXISTS exam_module_context (
	module_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	context JSONB NOT NULL, 
	context_hash TEXT NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (module_id), 
	FOREIGN KEY(module_id) REFERENCES course_modules (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_exam_module_context_course_id ON exam_module_context (course_id);

-- exam_generation_logs
CREATE TABLE IF NOT EXISTS exam_generation_logs (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	job_id UUID, 
	course_id UUID, 
	module_id UUID, 
	purpose TEXT NOT NULL, 
	provider TEXT, 
	model TEXT, 
	requested_count INTEGER DEFAULT '0' NOT NULL, 
	accepted INTEGER DEFAULT '0' NOT NULL, 
	rejected INTEGER DEFAULT '0' NOT NULL, 
	prompt_tokens INTEGER DEFAULT '0' NOT NULL, 
	completion_tokens INTEGER DEFAULT '0' NOT NULL, 
	rate_limits JSONB, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES exam_generation_jobs (id) ON DELETE SET NULL, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_exam_generation_logs_created ON exam_generation_logs (created_at);
CREATE INDEX IF NOT EXISTS ix_exam_generation_logs_job_id ON exam_generation_logs (job_id);

-- exam_attempts
CREATE TABLE IF NOT EXISTS exam_attempts (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	recipient_name TEXT NOT NULL, 
	question_ids UUID[] NOT NULL, 
	answers JSONB DEFAULT '{}'::jsonb NOT NULL, 
	tab_switches INTEGER DEFAULT '0' NOT NULL, 
	status TEXT NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	submitted_at TIMESTAMP WITH TIME ZONE, 
	score INTEGER, 
	total INTEGER NOT NULL, 
	passed BOOLEAN, 
	module_breakdown JSONB, 
	results JSONB, 
	lost_reason TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_exam_attempts_status CHECK (status in ('active','submitted','expired','lost')), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_exam_attempts_course_status ON exam_attempts (course_id, status);
CREATE INDEX IF NOT EXISTS ix_exam_attempts_question_ids ON exam_attempts USING gin (question_ids);
CREATE INDEX IF NOT EXISTS ix_exam_attempts_status_expires ON exam_attempts (status, expires_at);
CREATE INDEX IF NOT EXISTS ix_exam_attempts_user_course_submitted ON exam_attempts (user_id, course_id, submitted_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_attempts_user_course_active ON exam_attempts (user_id, course_id) WHERE status = 'active';

-- exam_attempt_cache
CREATE TABLE IF NOT EXISTS exam_attempt_cache (
	attempt_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	payload JSONB NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (attempt_id), 
	FOREIGN KEY(attempt_id) REFERENCES exam_attempts (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_exam_attempt_cache_expires_at ON exam_attempt_cache (expires_at);

-- certificates
CREATE TABLE IF NOT EXISTS certificates (
	certificate_id TEXT NOT NULL, 
	user_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	attempt_id UUID NOT NULL, 
	recipient_name TEXT NOT NULL, 
	course_title TEXT NOT NULL, 
	course_slug TEXT NOT NULL, 
	score INTEGER NOT NULL, 
	total INTEGER NOT NULL, 
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	status TEXT DEFAULT 'valid' NOT NULL, 
	revoked_reason TEXT, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	replaced_by TEXT, 
	PRIMARY KEY (certificate_id), 
	CONSTRAINT ck_certificates_status CHECK (status in ('valid','revoked')), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(attempt_id) REFERENCES exam_attempts (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_certificates_user_issued ON certificates (user_id, issued_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_certificates_attempt_valid ON certificates (attempt_id) WHERE status = 'valid';
CREATE UNIQUE INDEX IF NOT EXISTS uq_certificates_user_course_valid ON certificates (user_id, course_id) WHERE status = 'valid';

-- exam_question_reports
CREATE TABLE IF NOT EXISTS exam_question_reports (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	question_id UUID NOT NULL, 
	attempt_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	reason TEXT NOT NULL, 
	comment TEXT DEFAULT '' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	resolved_by UUID, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_exam_question_reports_attempt_question UNIQUE (attempt_id, question_id), 
	CONSTRAINT ck_exam_question_reports_reason CHECK (reason in ('wrong_answer','unclear','typo','other')), 
	FOREIGN KEY(question_id) REFERENCES exam_questions (id) ON DELETE CASCADE, 
	FOREIGN KEY(attempt_id) REFERENCES exam_attempts (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_exam_question_reports_question ON exam_question_reports (question_id, resolved_at);

-- exam_question_stats
CREATE TABLE IF NOT EXISTS exam_question_stats (
	question_id UUID NOT NULL, 
	answered INTEGER DEFAULT '0' NOT NULL, 
	correct INTEGER DEFAULT '0' NOT NULL, 
	discrimination FLOAT, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (question_id), 
	FOREIGN KEY(question_id) REFERENCES exam_questions (id) ON DELETE CASCADE
);
