-- CodeLab AI coding assistant: calls and tokens per user, per UTC day.
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS assistant_usage (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day date NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    input_tokens bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, provider, model)
);

CREATE INDEX IF NOT EXISTS ix_assistant_usage_day ON assistant_usage(day);
