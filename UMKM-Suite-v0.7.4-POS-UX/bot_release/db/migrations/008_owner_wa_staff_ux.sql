\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS staff_invites (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code_hash char(64) NOT NULL,
    full_name varchar(160) NOT NULL,
    role varchar(20) NOT NULL
        CHECK (role IN ('ADMIN','FINANCE','CASHIER','INVENTORY','VIEWER')),
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    claimed_by uuid REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_invites_unused_code
    ON staff_invites(code_hash) WHERE used_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_staff_invites_tenant_active
    ON staff_invites(tenant_id, expires_at DESC) WHERE used_at IS NULL;

-- Invitation rows can be resolved before the employee has a tenant context.
-- Therefore the application receives no direct table grant; it can only use
-- the two SECURITY DEFINER functions below, which validate actor and role.
REVOKE ALL ON TABLE staff_invites FROM PUBLIC;
REVOKE ALL ON TABLE staff_invites FROM umkm_app;

CREATE OR REPLACE FUNCTION app_create_staff_invite(
    p_code_hash text,
    p_full_name text,
    p_role text
)
RETURNS TABLE(invite_id uuid, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tenant uuid;
    v_user uuid;
    v_actor_role text;
    v_name text;
    v_target_role text;
    v_id uuid;
    v_expires timestamptz;
BEGIN
    v_tenant := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    v_user := NULLIF(current_setting('app.user_id', true), '')::uuid;
    v_name := btrim(COALESCE(p_full_name, ''));
    v_target_role := upper(btrim(COALESCE(p_role, '')));

    SELECT tm.role INTO v_actor_role
    FROM tenant_members tm
    JOIN users u ON u.id=tm.user_id
    JOIN tenants t ON t.id=tm.tenant_id
    WHERE tm.tenant_id=v_tenant
      AND tm.user_id=v_user
      AND tm.is_active=TRUE
      AND u.access_status='active'
      AND u.deleted_at IS NULL
      AND t.is_active=TRUE;

    IF v_actor_role NOT IN ('OWNER','ADMIN') THEN
        RAISE EXCEPTION 'STAFF_WRITE_DENIED';
    END IF;
    IF char_length(v_name) < 2 OR char_length(v_name) > 160 THEN
        RAISE EXCEPTION 'INVALID_STAFF_NAME';
    END IF;
    IF v_target_role NOT IN ('ADMIN','FINANCE','CASHIER','INVENTORY','VIEWER') THEN
        RAISE EXCEPTION 'INVALID_STAFF_ROLE';
    END IF;
    IF v_actor_role='ADMIN' AND v_target_role='ADMIN' THEN
        RAISE EXCEPTION 'ADMIN_CANNOT_CREATE_ADMIN';
    END IF;
    IF p_code_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'INVALID_INVITE_HASH';
    END IF;

    DELETE FROM staff_invites si
    WHERE si.used_at IS NULL AND si.expires_at <= now();

    v_expires := now() + interval '30 minutes';
    INSERT INTO staff_invites(
        tenant_id,code_hash,full_name,role,created_by,expires_at
    ) VALUES (
        v_tenant,p_code_hash,v_name,v_target_role,v_user,v_expires
    )
    RETURNING id INTO v_id;

    INSERT INTO audit_logs(
        tenant_id,user_id,action,object_type,object_id,new_value
    ) VALUES (
        v_tenant,v_user,'CREATE_STAFF_INVITE','staff_invite',v_id,
        jsonb_build_object('full_name',v_name,'role',v_target_role,'expires_at',v_expires)
    );

    RETURN QUERY SELECT v_id,v_expires;
END;
$$;

CREATE OR REPLACE FUNCTION app_claim_staff_invite(
    p_whatsapp_jid text,
    p_code_hash text
)
RETURNS TABLE(
    user_id uuid,
    tenant_id uuid,
    full_name text,
    business_name text,
    role text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_user users%ROWTYPE;
    v_invite staff_invites%ROWTYPE;
    v_business text;
BEGIN
    SELECT u.* INTO v_user
    FROM whatsapp_identities wi
    JOIN users u ON u.id=wi.user_id
    WHERE wi.whatsapp_jid=p_whatsapp_jid
      AND wi.is_active=TRUE
    FOR UPDATE OF u;

    IF NOT FOUND OR v_user.is_master THEN
        RAISE EXCEPTION 'WA_USER_NOT_FOUND';
    END IF;

    SELECT * INTO v_invite
    FROM staff_invites
    WHERE code_hash=p_code_hash
      AND used_at IS NULL
      AND expires_at > now()
    ORDER BY created_at DESC
    LIMIT 1
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'INVITE_NOT_FOUND_OR_EXPIRED';
    END IF;

    IF v_user.active_tenant_id IS NOT NULL
       OR v_user.access_status='active' THEN
        RAISE EXCEPTION 'ACCOUNT_ALREADY_ASSIGNED';
    END IF;

    UPDATE users
    SET full_name=v_invite.full_name,
        access_status='active',
        active_tenant_id=v_invite.tenant_id,
        approved_at=now(),
        approved_by=v_invite.created_by,
        deleted_at=NULL,
        updated_at=now()
    WHERE id=v_user.id;

    INSERT INTO tenant_members(tenant_id,user_id,role,is_active)
    VALUES (v_invite.tenant_id,v_user.id,v_invite.role,TRUE)
    ON CONFLICT (tenant_id,user_id)
    DO UPDATE SET role=EXCLUDED.role,is_active=TRUE;

    UPDATE staff_invites
    SET used_at=now(),claimed_by=v_user.id
    WHERE id=v_invite.id;

    SELECT t.business_name INTO v_business
    FROM tenants t WHERE t.id=v_invite.tenant_id;

    INSERT INTO audit_logs(
        tenant_id,user_id,action,object_type,object_id,new_value
    ) VALUES (
        v_invite.tenant_id,v_invite.created_by,'CLAIM_STAFF_INVITE','user',v_user.id,
        jsonb_build_object('full_name',v_invite.full_name,'role',v_invite.role,'channel','whatsapp')
    );

    RETURN QUERY
    SELECT v_user.id,v_invite.tenant_id,v_invite.full_name::text,
           v_business,v_invite.role::text;
END;
$$;

REVOKE ALL ON FUNCTION app_create_staff_invite(text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_claim_staff_invite(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_create_staff_invite(text,text,text) TO umkm_app;
GRANT EXECUTE ON FUNCTION app_claim_staff_invite(text,text) TO umkm_app;

-- MASTER sees business owners (plus unapproved applicants). Employees remain
-- under their own business and are managed by OWNER/ADMIN instead.
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
        u.id,u.telegram_user_id,u.full_name,u.access_status,u.is_master,
        t.business_name,tm.role,u.created_at
    FROM users u
    LEFT JOIN tenant_members tm
      ON tm.user_id=u.id
     AND tm.tenant_id=u.active_tenant_id
     AND tm.is_active=TRUE
    LEFT JOIN tenants t ON t.id=u.active_tenant_id
    WHERE u.deleted_at IS NULL
      AND (
          u.is_master=TRUE
          OR u.access_status='pending'
          OR tm.role='OWNER'
      )
    ORDER BY
        CASE u.access_status WHEN 'pending' THEN 0 ELSE 1 END,
        u.created_at DESC;
END;
$$;

INSERT INTO schema_migrations(version,description)
VALUES ('008','OWNER business list and WhatsApp guided staff/POS UX')
ON CONFLICT (version) DO NOTHING;

COMMIT;
