CREATE TABLE IF NOT EXISTS java_playground_daily_usage (
    day date PRIMARY KEY,
    runs integer NOT NULL DEFAULT 0
);
