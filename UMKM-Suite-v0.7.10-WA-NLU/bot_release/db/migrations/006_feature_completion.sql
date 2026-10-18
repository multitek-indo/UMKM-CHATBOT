-- UMKM Suite v0.6.0 - feature completion
-- Additive migration: recurring reminders + channel linking.

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS recurrence_type VARCHAR(20) NOT NULL DEFAULT 'once';

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS recurrence_interval INTEGER NOT NULL DEFAULT 1;

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS recurrence_day SMALLINT;

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS recurrence_weekday SMALLINT;

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS recurrence_time TIME;

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS schedule_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Jakarta';

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS last_sent_at TIMESTAMPTZ;

ALTER TABLE reminders
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'reminders_recurrence_type_check'
    ) THEN
        ALTER TABLE reminders
        ADD CONSTRAINT reminders_recurrence_type_check
        CHECK (recurrence_type IN ('once','daily','weekly','monthly'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'reminders_recurrence_interval_check'
    ) THEN
        ALTER TABLE reminders
        ADD CONSTRAINT reminders_recurrence_interval_check
        CHECK (recurrence_interval >= 1 AND recurrence_interval <= 365);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'reminders_recurrence_day_check'
    ) THEN
        ALTER TABLE reminders
        ADD CONSTRAINT reminders_recurrence_day_check
        CHECK (recurrence_day IS NULL OR recurrence_day BETWEEN 1 AND 31);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'reminders_recurrence_weekday_check'
    ) THEN
        ALTER TABLE reminders
        ADD CONSTRAINT reminders_recurrence_weekday_check
        CHECK (recurrence_weekday IS NULL OR recurrence_weekday BETWEEN 0 AND 6);
    END IF;
END $$;

-- Legacy versions marked sent reminders using sent_at but left status='pending'.
-- Normalize them before the recurring-safe claim functions ignore sent_at.
UPDATE reminders
SET status='done', updated_at=NOW()
WHERE status='pending'
  AND sent_at IS NOT NULL
  AND COALESCE(recurrence_type, 'once')='once';

CREATE INDEX IF NOT EXISTS idx_reminders_active_channel_due
    ON reminders(delivery_channel, status, due_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS channel_link_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_channel_link_codes_hash
    ON channel_link_codes(code_hash, expires_at)
    WHERE used_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_link_codes_hash_unused
    ON channel_link_codes(code_hash)
    WHERE used_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_channel_link_codes_user
    ON channel_link_codes(user_id, created_at DESC);

REVOKE ALL ON TABLE channel_link_codes FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON channel_link_codes TO :"app_user";

-- Advance one-time/recurring reminders after successful delivery.
CREATE OR REPLACE FUNCTION advance_reminder_after_send(p_reminder_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_type TEXT;
    v_due TIMESTAMPTZ;
    v_interval INTEGER;
    v_day INTEGER;
    v_weekday INTEGER;
    v_clock TIME;
    v_tz TEXT;
    v_local TIMESTAMP;
    v_now_local TIMESTAMP;
    v_month_start DATE;
    v_last_day INTEGER;
    v_target_day INTEGER;
    v_guard INTEGER := 0;
BEGIN
    SELECT
        recurrence_type,
        due_at,
        GREATEST(COALESCE(recurrence_interval, 1), 1),
        recurrence_day,
        recurrence_weekday,
        recurrence_time,
        COALESCE(NULLIF(schedule_timezone, ''), 'Asia/Jakarta')
    INTO
        v_type, v_due, v_interval, v_day, v_weekday, v_clock, v_tz
    FROM reminders
    WHERE id = p_reminder_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF COALESCE(v_type, 'once') = 'once' THEN
        UPDATE reminders
        SET
            status = 'done',
            sent_at = NOW(),
            last_sent_at = NOW(),
            updated_at = NOW()
        WHERE id = p_reminder_id;
        RETURN;
    END IF;

    v_local := v_due AT TIME ZONE v_tz;
    v_now_local := NOW() AT TIME ZONE v_tz;

    LOOP
        v_guard := v_guard + 1;
        IF v_guard > 500 THEN
            RAISE EXCEPTION 'REMINDER_ADVANCE_GUARD';
        END IF;

        IF v_type = 'daily' THEN
            v_local := v_local + make_interval(days => v_interval);

        ELSIF v_type = 'weekly' THEN
            v_local := v_local + make_interval(days => 7 * v_interval);

        ELSIF v_type = 'monthly' THEN
            v_month_start := (
                date_trunc('month', v_local)
                + make_interval(months => v_interval)
            )::date;

            v_last_day := EXTRACT(
                DAY FROM (
                    date_trunc('month', v_month_start::timestamp)
                    + interval '1 month - 1 day'
                )
            )::integer;

            v_target_day := LEAST(
                COALESCE(v_day, EXTRACT(DAY FROM v_local)::integer),
                v_last_day
            );

            v_local :=
                (v_month_start + (v_target_day - 1))
                + COALESCE(v_clock, v_local::time);
        ELSE
            RAISE EXCEPTION 'INVALID_RECURRENCE_TYPE';
        END IF;

        EXIT WHEN v_local > v_now_local;
    END LOOP;

    UPDATE reminders
    SET
        due_at = v_local AT TIME ZONE v_tz,
        status = 'pending',
        sent_at = NULL,
        last_sent_at = NOW(),
        updated_at = NOW()
    WHERE id = p_reminder_id;
END;
$$;

-- Telegram worker: only Telegram reminders, recurring-safe.
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
      AND r.due_at <= NOW()
      AND COALESCE(r.delivery_channel, 'telegram') = 'telegram'
      AND u.telegram_user_id IS NOT NULL
      AND u.access_status = 'active'
      AND u.deleted_at IS NULL
      AND u.is_master = FALSE
    ORDER BY r.due_at ASC
    LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

CREATE OR REPLACE FUNCTION worker_mark_reminder_sent(p_reminder_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM advance_reminder_after_send(p_reminder_id);
END;
$$;

-- WhatsApp worker: only WhatsApp reminders, recurring-safe.
CREATE OR REPLACE FUNCTION wa_claim_due_reminders(p_limit integer DEFAULT 50)
RETURNS TABLE (
    reminder_id UUID,
    whatsapp_jid TEXT,
    message TEXT,
    due_at TIMESTAMPTZ
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT
        r.id,
        COALESCE(NULLIF(r.delivery_target, ''), wi.whatsapp_jid),
        r.message,
        r.due_at
    FROM reminders r
    JOIN users u
      ON u.id = r.created_by
     AND u.access_status = 'active'
     AND u.deleted_at IS NULL
     AND u.is_master = FALSE
    JOIN whatsapp_identities wi
      ON wi.user_id = u.id
     AND wi.is_active = TRUE
    WHERE r.status = 'pending'
      AND r.due_at <= NOW()
      AND r.delivery_channel = 'whatsapp'
    ORDER BY r.due_at ASC
    LIMIT LEAST(GREATEST(p_limit, 1), 100);
$$;

CREATE OR REPLACE FUNCTION wa_mark_reminder_sent(p_reminder_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    PERFORM advance_reminder_after_send(p_reminder_id);
END;
$$;

REVOKE ALL ON FUNCTION advance_reminder_after_send(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION worker_claim_due_reminders(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION worker_mark_reminder_sent(UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION wa_claim_due_reminders(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION wa_mark_reminder_sent(UUID) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION worker_claim_due_reminders(integer) TO :"worker_user";
GRANT EXECUTE ON FUNCTION worker_mark_reminder_sent(UUID) TO :"worker_user";
GRANT EXECUTE ON FUNCTION wa_claim_due_reminders(integer) TO :"app_user";
GRANT EXECUTE ON FUNCTION wa_mark_reminder_sent(UUID) TO :"app_user";
