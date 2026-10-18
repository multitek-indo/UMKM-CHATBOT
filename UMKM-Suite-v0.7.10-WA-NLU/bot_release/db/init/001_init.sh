#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_DB:?}"
: "${POSTGRES_USER:?}"
: "${APP_DB_USER:?}"
: "${APP_DB_PASSWORD:?}"
: "${WORKER_DB_USER:?}"
: "${WORKER_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_user="$APP_DB_USER" \
  --set=app_password="$APP_DB_PASSWORD" \
  --set=worker_user="$WORKER_DB_USER" \
  --set=worker_password="$WORKER_DB_PASSWORD" <<'EOSQL'

CREATE EXTENSION IF NOT EXISTS pgcrypto;

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'app_user'
)\gexec

SELECT format(
    'ALTER ROLE %I PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'app_user',
    :'app_password'
)\gexec

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'worker_user',
    :'worker_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'worker_user'
)\gexec

SELECT format(
    'ALTER ROLE %I PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
    :'worker_user',
    :'worker_password'
)\gexec

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT NOT NULL UNIQUE,
    full_name TEXT,
    active_tenant_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_name VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_active_tenant_id_fkey;

ALTER TABLE users
    ADD CONSTRAINT users_active_tenant_id_fkey
    FOREIGN KEY (active_tenant_id)
    REFERENCES tenants(id)
    ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS tenant_members (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL CHECK (role IN ('OWNER','ADMIN','FINANCE','CASHIER','VIEWER')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_members_user
    ON tenant_members(user_id, is_active);

CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    type VARCHAR(10) NOT NULL CHECK (type IN ('income','expense')),
    amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    category VARCHAR(100) NOT NULL DEFAULT 'Umum',
    description TEXT NOT NULL,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    status VARCHAR(20) NOT NULL DEFAULT 'posted'
        CHECK (status IN ('posted','void')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_tenant_date
    ON transactions(tenant_id, transaction_date DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','done','cancelled')),
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(status, sent_at, due_at);

CREATE INDEX IF NOT EXISTS idx_reminders_tenant_due
    ON reminders(tenant_id, due_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(40) NOT NULL,
    object_type VARCHAR(40) NOT NULL,
    object_id UUID,
    old_value JSONB,
    new_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_created
    ON audit_logs(tenant_id, created_at DESC);

-- RLS: data bisnis tidak dapat terbaca tanpa tenant context yang benar.
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions FORCE ROW LEVEL SECURITY;

ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders FORCE ROW LEVEL SECURITY;

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS transactions_tenant_isolation ON transactions;
CREATE POLICY transactions_tenant_isolation
ON transactions
FOR ALL
USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)
WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
);

DROP POLICY IF EXISTS reminders_tenant_isolation ON reminders;
CREATE POLICY reminders_tenant_isolation
ON reminders
FOR ALL
USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)
WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
);

DROP POLICY IF EXISTS audit_tenant_isolation ON audit_logs;
CREATE POLICY audit_tenant_isolation
ON audit_logs
FOR ALL
USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
)
WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
);

-- Scheduler lintas tenant hanya boleh mengambil field reminder yang diperlukan.
CREATE OR REPLACE FUNCTION worker_claim_due_reminders(p_limit integer DEFAULT 50)
RETURNS TABLE (
    reminder_id UUID,
    telegram_user_id BIGINT,
    message TEXT,
    due_at TIMESTAMPTZ
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT
        r.id,
        u.telegram_user_id,
        r.message,
        r.due_at
    FROM reminders r
    JOIN users u ON u.id = r.created_by
    WHERE r.status = 'pending'
      AND r.sent_at IS NULL
      AND r.due_at <= NOW()
    ORDER BY r.due_at ASC
    LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

CREATE OR REPLACE FUNCTION worker_mark_reminder_sent(p_reminder_id UUID)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE reminders
    SET sent_at = NOW()
    WHERE id = p_reminder_id
      AND sent_at IS NULL;
$$;

REVOKE ALL ON FUNCTION worker_claim_due_reminders(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION worker_mark_reminder_sent(UUID) FROM PUBLIC;

-- Hak aplikasi.
GRANT CONNECT ON DATABASE :"DBNAME" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE ON users, tenants, tenant_members TO :"app_user";
GRANT SELECT, INSERT, UPDATE ON transactions, reminders TO :"app_user";
GRANT SELECT, INSERT ON audit_logs TO :"app_user";
GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO :"app_user";

-- Worker tidak diberi akses langsung ke tabel.
GRANT CONNECT ON DATABASE :"DBNAME" TO :"worker_user";
GRANT USAGE ON SCHEMA public TO :"worker_user";
GRANT EXECUTE ON FUNCTION worker_claim_due_reminders(integer) TO :"worker_user";
GRANT EXECUTE ON FUNCTION worker_mark_reminder_sent(UUID) TO :"worker_user";

EOSQL
