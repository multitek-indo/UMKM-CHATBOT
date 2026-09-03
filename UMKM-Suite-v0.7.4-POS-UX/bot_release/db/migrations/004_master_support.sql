CREATE TABLE IF NOT EXISTS support_sessions (
    master_user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_support_sessions_target
    ON support_sessions(target_user_id);

CREATE OR REPLACE FUNCTION admin_get_support()
RETURNS TABLE (
    target_user_id UUID,
    target_telegram_user_id BIGINT,
    target_full_name TEXT,
    business_name VARCHAR,
    tenant_id UUID,
    role VARCHAR,
    started_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_master UUID;
BEGIN
    PERFORM admin_master_required();

    SELECT u.id
    INTO v_master
    FROM users u
    WHERE u.telegram_user_id =
          NULLIF(current_setting('app.telegram_user_id', true), '')::BIGINT
      AND u.is_master = TRUE
      AND u.access_status = 'active'
      AND u.deleted_at IS NULL;

    RETURN QUERY
    SELECT
        ss.target_user_id,
        tu.telegram_user_id,
        tu.full_name,
        t.business_name,
        tu.active_tenant_id,
        tm.role,
        ss.started_at
    FROM support_sessions ss
    JOIN users tu
      ON tu.id = ss.target_user_id
     AND tu.is_master = FALSE
     AND tu.access_status = 'active'
     AND tu.deleted_at IS NULL
    JOIN tenants t
      ON t.id = tu.active_tenant_id
     AND t.is_active = TRUE
    JOIN tenant_members tm
      ON tm.user_id = tu.id
     AND tm.tenant_id = tu.active_tenant_id
     AND tm.is_active = TRUE
    WHERE ss.master_user_id = v_master
    LIMIT 1;
END;
$$;

CREATE OR REPLACE FUNCTION admin_start_support(p_target_user_id UUID)
RETURNS TABLE (
    target_user_id UUID,
    target_telegram_user_id BIGINT,
    target_full_name TEXT,
    business_name VARCHAR,
    tenant_id UUID,
    role VARCHAR,
    started_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_master UUID;
    v_tenant UUID;
BEGIN
    PERFORM admin_master_required();

    SELECT u.id
    INTO v_master
    FROM users u
    WHERE u.telegram_user_id =
          NULLIF(current_setting('app.telegram_user_id', true), '')::BIGINT
      AND u.is_master = TRUE
      AND u.access_status = 'active'
      AND u.deleted_at IS NULL;

    SELECT tu.active_tenant_id
    INTO v_tenant
    FROM users tu
    JOIN tenants t
      ON t.id = tu.active_tenant_id
     AND t.is_active = TRUE
    JOIN tenant_members tm
      ON tm.user_id = tu.id
     AND tm.tenant_id = tu.active_tenant_id
     AND tm.is_active = TRUE
    WHERE tu.id = p_target_user_id
      AND tu.is_master = FALSE
      AND tu.access_status = 'active'
      AND tu.deleted_at IS NULL;

    IF v_tenant IS NULL THEN
        RAISE EXCEPTION 'TARGET_USER_NOT_ACTIVE';
    END IF;

    INSERT INTO support_sessions (
        master_user_id,
        target_user_id,
        started_at,
        updated_at
    )
    VALUES (
        v_master,
        p_target_user_id,
        NOW(),
        NOW()
    )
    ON CONFLICT (master_user_id)
    DO UPDATE SET
        target_user_id = EXCLUDED.target_user_id,
        started_at = NOW(),
        updated_at = NOW();

    INSERT INTO audit_logs (
        tenant_id,
        user_id,
        action,
        object_type,
        object_id,
        new_value
    )
    VALUES (
        v_tenant,
        v_master,
        'SUPPORT_LOGIN',
        'user',
        p_target_user_id,
        jsonb_build_object(
            'support_mode', TRUE,
            'started_at', NOW()
        )
    );

    RETURN QUERY
    SELECT
        tu.id,
        tu.telegram_user_id,
        tu.full_name,
        t.business_name,
        tu.active_tenant_id,
        tm.role,
        ss.started_at
    FROM support_sessions ss
    JOIN users tu ON tu.id = ss.target_user_id
    JOIN tenants t ON t.id = tu.active_tenant_id
    JOIN tenant_members tm
      ON tm.user_id = tu.id
     AND tm.tenant_id = tu.active_tenant_id
     AND tm.is_active = TRUE
    WHERE ss.master_user_id = v_master
    LIMIT 1;
END;
$$;

CREATE OR REPLACE FUNCTION admin_stop_support()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_master UUID;
    v_target UUID;
    v_tenant UUID;
BEGIN
    PERFORM admin_master_required();

    SELECT u.id
    INTO v_master
    FROM users u
    WHERE u.telegram_user_id =
          NULLIF(current_setting('app.telegram_user_id', true), '')::BIGINT
      AND u.is_master = TRUE
      AND u.access_status = 'active'
      AND u.deleted_at IS NULL;

    SELECT ss.target_user_id, tu.active_tenant_id
    INTO v_target, v_tenant
    FROM support_sessions ss
    JOIN users tu ON tu.id = ss.target_user_id
    WHERE ss.master_user_id = v_master;

    IF v_target IS NOT NULL AND v_tenant IS NOT NULL THEN
        INSERT INTO audit_logs (
            tenant_id,
            user_id,
            action,
            object_type,
            object_id,
            new_value
        )
        VALUES (
            v_tenant,
            v_master,
            'SUPPORT_LOGOUT',
            'user',
            v_target,
            jsonb_build_object(
                'support_mode', FALSE,
                'ended_at', NOW()
            )
        );
    END IF;

    DELETE FROM support_sessions
    WHERE master_user_id = v_master;
END;
$$;

REVOKE ALL ON TABLE support_sessions FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_get_support() FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_start_support(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION admin_stop_support() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION admin_get_support() TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_start_support(UUID) TO :"app_user";
GRANT EXECUTE ON FUNCTION admin_stop_support() TO :"app_user";
