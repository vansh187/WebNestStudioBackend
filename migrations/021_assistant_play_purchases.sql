-- assistant_play_purchases: WebNest Prime bought through Google Play in the Android app. One
-- row per purchase token, which is unique, so a token can only ever grant once.
--
-- user_id is ON DELETE SET NULL, like assistant_payments, so DELETE /api/auth/me keeps working
-- for a member who paid and the purchase record survives, unlinked.
--
-- A new, empty table, so creating it takes no lock that matters. Safe to re-run.
--
-- Rollback:
--   drop table if exists assistant_play_purchases;

BEGIN;

SET LOCAL lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS assistant_play_purchases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    purchase_token text NOT NULL UNIQUE,
    google_order_id text,
    product_id text NOT NULL,
    status text NOT NULL DEFAULT 'granted',
    pass_days integer NOT NULL,
    pass_tokens bigint NOT NULL,
    region_code text,
    purchase_time timestamptz,
    terms_version text,
    terms_accepted_at timestamptz,
    acknowledged_at timestamptz,
    granted_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_assistant_play_purchase_status CHECK (status IN ('granted'))
);

CREATE INDEX IF NOT EXISTS ix_assistant_play_purchases_user ON assistant_play_purchases (user_id);
CREATE INDEX IF NOT EXISTS ix_assistant_play_purchases_order ON assistant_play_purchases (google_order_id);

-- Only the API, which owns the table, may touch it. No policies, so anon and authenticated
-- get nothing even if a grant were ever added by mistake.
ALTER TABLE assistant_play_purchases ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON assistant_play_purchases FROM anon, authenticated;

COMMIT;
