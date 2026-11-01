-- assistant_access: when the current run of passes began. A refund uses it to tell whether
-- the refunded payment's pass is still in effect, or lapsed before a newer one started.
--
-- Safe to re-run. Adding a column with a default doesn't rewrite the table. If something
-- holds a lock on it, this gives up after 3 seconds, so just run it again.
--
-- Rollback:
--   alter table assistant_access drop column if exists period_started_at;

BEGIN;

SET LOCAL lock_timeout = '3s';

ALTER TABLE assistant_access ADD COLUMN IF NOT EXISTS period_started_at timestamptz NOT NULL DEFAULT now();

-- Passes bought before this column existed started when they were paid for, not when it was added.
UPDATE assistant_access a
SET period_started_at = p.first_paid_at
FROM (
    SELECT user_id, min(paid_at) AS first_paid_at
    FROM assistant_payments
    WHERE status = 'paid' AND paid_at IS NOT NULL AND user_id IS NOT NULL
    GROUP BY user_id
) p
WHERE p.user_id = a.user_id AND p.first_paid_at < a.period_started_at;

COMMIT;
