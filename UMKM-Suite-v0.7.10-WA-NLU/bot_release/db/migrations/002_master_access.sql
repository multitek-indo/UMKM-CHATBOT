ALTER TABLE users
    ADD COLUMN IF NOT EXISTS access_status VARCHAR(20) NOT NULL DEFAULT 'pending';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_master BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS approved_by UUID;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'users_access_status_check'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_access_status_check
            CHECK (access_status IN ('pending','active','suspended','deleted'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_access_status
    ON users(access_status, created_at DESC);

-- MASTER yang dipilih selalu aktif.
UPDATE users
SET
    is_master = TRUE,
    access_status = 'active',
    approved_at = COALESCE(approved_at, NOW()),
    deleted_at = NULL
WHERE telegram_user_id = :'master_id'::BIGINT;

-- Pastikan hanya Telegram ID terpilih yang menjadi master.
UPDATE users
SET is_master = FALSE
WHERE telegram_user_id <> :'master_id'::BIGINT
  AND is_master = TRUE;

CREATE OR REPLACE FUNCTION admin_master_required()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tg BIGINT;
BEGIN
    v_tg := NULLIF(current_setting('app.telegram_user_id', true), '')::BIGINT;

    IF v_tg IS NULL OR NOT EXISTS (
        SELECT 1
        FROM users
        WHERE telegram_user_id = v_tg
          AND is_master = TRUE
          AND access_status = 'active'
          AND deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'MASTER_REQUIRED';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION admin_list_users()
RETURNS TABLE (
    user_id UUID,
    telegram_user_id BIGINT,
    full_name TEXT,
    access_status VARCHAR,
    is_master BOOLEAN,
    business_name VARCHAR,
    role VARCHAR,
    created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM admin_master_required();

    RETURN QUERY
    SELECT
        u.id,
        u.telegram_user_id,
        u.full_name,
        u.access_status,
        u.is_master,
        t.business_name,
        tm.role,
        u.created_at
    FROM users u
    LEFT JOIN tenant_members tm
      ON tm.user_id = u.id
     AND tm.is_active = TRUE
    LEFT JOIN tenants t
      ON t.id = tm.tenant_id
    WHERE u.deleted_at IS NULL
    ORDER BY
        CASE u.access_status WHEN 'pending' THEN 0 ELSE 1 END,
        u.created_at DESC;
END;
$$;

CREATE OR REPLACE FUNCTION admin_approve_user(
    p_user_id UUID,
    p_business_name TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_master UUID;
    v_tenant UUID;
    v_name TEXT;
BEGIN
    PERFORM admin_master_required();

    v_name := btrim(p_business_name);
    IF char_length(v_name) < 2 OR char_length(v_name) > 100 THEN
        RAISE EXCEPTION 'INVALID_BUSINESS_NAME';
    END IF;

    SELECT id INTO v_master
    FROM users
    WHERE telegram_user_id =
          NULLIF(current_setting('app.telegram_user_id', true), '')::BIGINT
      AND is_master = TRUE;

    IF NOT EXISTS (
        SELECT 1 FROM users
        WHERE id = p_user_id
          AND is_master = FALSE
          AND deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'USER_NOT_FOUND';
    END IF;

    SELECT u.active_tenant_id
    INTO v_tenant
    FROM users u
    WHERE u.id = p_user_id;

    IF v_tenant IS NULL THEN
        INSERT INTO tenants (business_name, is_active)
        VALUES (v_name, TRUE)
        RETURNING id INTO v_tenant;

        INSERT INTO tenant_members (tenant_id, user_id, role, is_active)
        VALUES (v_tenant, p_user_id, 'OWNER', TRUE)
        ON CONFLICT (tenant_id, user_id)
        DO UPDATE SET role = 'OWNER', is_active = TRUE;
    ELSE
        UPDATE tenants
        SET business_name = v_name, is_active = TRUE
        WHERE id = v_tenant;

        INSERT INTO tenant_members (tenant_id, user_id, role, is_active)
        VALUES (v_tenant, p_user_id, 'OWNER', TRUE)
        ON CONFLICT (tenant_id, user_id)
        DO UPDATE SET role = 'OWNER', is_active = TRUE;
    END IF;

    UPDATE users
    SET
        active_tenant_id = v_tenant,
        access_status = 'active',
        approved_at = NOW(),
        approved_by = v_master,
        deleted_at = NULL,
        updated_at = NOW()
    WHERE id = p_user_id;

    RETURN v_tenant;
END;
$$;

CREATE OR REPLACE FUNCTION admin_precreate_user(
    p_telegram_user_id BIGINT,
    p_full_name TEXT,
    p_business_name TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_user UUID;
BEGIN
    PERFORM admin_master_required();

    IF p_telegram_user_id <= 0 THEN
        RAISE EXCEPTION 'INVALID_TELEGRAM_ID';
    END IF;

    IF EXISTS (
        SELECT 1 FROM users
        WHERE telegram_user_id = p_telegram_user_id
          AND is_master = TRUE
    ) THEN
        RAISE EXCEPTION 'CANNOT_MANAGE_MASTER';
    END IF;

    INSERT INTO users (
        telegram_user_id,
        full_name,
        access_status,
        deleted_at
    )
    VALUES (
        p_telegram_user_id,
        NULLIF(btrim(p_full_name), ''),
        'pending',
        NULL
    )
    ON CONFLICT (telegram_user_id)
    DO UPDATE SET
        full_name = COALESCE(NULLIF(btrim(EXCLUDED.full_name), ''), users.full_name),
        access_status = CASE
            WHEN users.access_status = 'deleted' THEN 'pending'
            ELSE users.access_status
        END,
        deleted_at = NULL,
        updated_at = NOW()
    RETURNING id INTO v_user;

    PERFORM admin_approve_user(v_user, p_business_name);
    RETURN v_user;
END;
$$;

CREATE OR REPLACE FUNCTION admin_set_user_status(
    p_user_id UUID,
    p_status TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM admin_master_required();

    IF p_status NOT IN ('pending','active','suspended','deleted') THEN
        RAISE EXCEPTION 'INVALID_STATUS';
    END IF;

    IF EXISTS (
        SELECT 1 FROM users
        WHERE id = p_user_id
          AND is_master = TRUE
    ) THEN
        RAISE EXCEPTION 'CANNOT_MANAGE_MASTER';
    END IF;

    UPDATE users
    SET
        access_status = p_status,
        deleted_at = CASE WHEN p_status = 'deleted' THEN NOW() ELSE NULL END,
        updated_at = NOW()
    WHERE id = p_user_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'USER_NOT_FOUND';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION admin_rename_user_business(
    p_user_id UUID,
    p_business_name TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant UUID;
    v_name TEXT;
BEGIN
    PERFORM admin_master_required();

    v_name := btrim(p_business_name);
    IF char_length(v_name) < 2 OR char_length(v_name) > 100 THEN
        RAISE EXCEPTION 'INVALID_BUSINESS_NAME';
    END IF;

    SELECT active_tenant_id INTO v_tenant
    FROM users
    WHERE id = p_user_id
      AND is_master = FALSE
      AND deleted_at IS NULL;

    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'TENANT_NOT_FOUND';
    END IF;

    UPDATE tenants
    SET business_name = v_name
    WHERE id = v_tenant;
END;
$$;

-- Worker reminder hanya mengirim ke user aktif.
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
      AND u.access_status = 'active'
      AND u.deleted_at IS NULL
      AND u.is_master = FALSE
    ORDER BY r.due_at ASC
    LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

REVOKE ALL ON FUNCTION worker_claim_due_reminders(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION worker_claim_due_reminders(integer) TO :"worker_user";

REVOKE ALL ON FUNCTION admin_master_required() FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_list_users() FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_approve_user(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_precreate_user(BIGINT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_set_user_status(UUID, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_rename_user_business(UUID, TEXT) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION admin_master_required() TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_list_users() TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_approve_user(UUID, TEXT) TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_precreate_user(BIGINT, TEXT, TEXT) TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_set_user_status(UUID, TEXT) TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_rename_user_business(UUID, TEXT) TO :"app_user";
