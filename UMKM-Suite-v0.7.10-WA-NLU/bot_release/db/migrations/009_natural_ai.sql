\set ON_ERROR_STOP on
BEGIN;

-- Additive conversation store. All existing financial and POS tables remain unchanged.
CREATE TABLE IF NOT EXISTS ai_dialogues (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel VARCHAR(20) NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
    member_role VARCHAR(20) NOT NULL,
    messages JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(messages) = 'array' AND jsonb_array_length(messages) <= 16),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(tenant_id,user_id,channel)
);

ALTER TABLE ai_dialogues ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_dialogues FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ai_dialogues_owner_isolation ON ai_dialogues;
CREATE POLICY ai_dialogues_owner_isolation ON ai_dialogues
FOR ALL
USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);

REVOKE ALL ON ai_dialogues FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON ai_dialogues TO umkm_app;

INSERT INTO schema_migrations(version,description)
VALUES ('009_natural_ai','Short-lived, role-scoped AI conversation memory')
ON CONFLICT (version) DO NOTHING;

COMMIT;
