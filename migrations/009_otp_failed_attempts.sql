-- Wrong-guess counter per OTP. After a few misses the code is locked
-- (marked consumed), so a 6-digit code cannot be brute-forced.
--
-- The app also creates this table at startup (create_all), so running this
-- by hand is optional. Safe to re-run.

create table if not exists otp_failed_attempts (
    otp_id          uuid primary key references otp_verifications(id) on delete cascade,
    failed_attempts integer not null default 0,
    updated_at      timestamptz default now()
);
