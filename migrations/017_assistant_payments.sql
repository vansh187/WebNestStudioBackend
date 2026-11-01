-- WebNest Prime Member: Razorpay payments, the access each one gives, and webhook dedupe.
-- The project owner approves this before it's applied to Supabase.
-- Apply it before deploying: startup's create_all would otherwise build these tables
-- without row level security.
--
-- assistant_payments.user_id is SET NULL, not RESTRICT: payment records outlive a deleted
-- account, and RESTRICT would make DELETE /api/auth/me fail for anyone who ever paid.
--
-- Row level security with no policies: the backend connects as the owner, which bypasses
-- it, and Supabase's anon and authenticated roles get nothing.
--
-- Safe to re-run. The foreign keys briefly lock users: if something holds that lock, this
-- gives up after 3 seconds instead of making logins queue, so just run it again.
--
-- Rollback:
--   drop table if exists assistant_webhook_events, assistant_access, assistant_payments;

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS assistant_payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    razorpay_order_id text NOT NULL UNIQUE,
    razorpay_payment_id text UNIQUE,
    amount_paise integer NOT NULL,
    currency text NOT NULL DEFAULT 'INR',
    status text NOT NULL DEFAULT 'created'
        CONSTRAINT ck_assistant_payment_status CHECK (status IN ('created', 'failed', 'paid', 'refunded')),
    pass_days integer NOT NULL,
    pass_tokens bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    paid_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_assistant_payments_user ON assistant_payments(user_id);
CREATE INDEX IF NOT EXISTS ix_assistant_payments_status_created ON assistant_payments(status, created_at);

CREATE TABLE IF NOT EXISTS assistant_access (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    tokens_limit bigint NOT NULL,
    tokens_used bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assistant_webhook_events (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE assistant_payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistant_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistant_webhook_events ENABLE ROW LEVEL SECURITY;

COMMIT;
