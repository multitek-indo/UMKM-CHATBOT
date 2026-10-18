\set ON_ERROR_STOP on

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version varchar(40) PRIMARY KEY,
    description text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- New inventory role. Existing tenants/users remain untouched.
ALTER TABLE tenant_members DROP CONSTRAINT IF EXISTS tenant_members_role_check;
ALTER TABLE tenant_members ADD CONSTRAINT tenant_members_role_check
    CHECK (role IN ('OWNER','ADMIN','FINANCE','CASHIER','INVENTORY','VIEWER'));

-- Link financial entries to POS without replacing the v0.6 ledger.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reference_type varchar(40);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reference_id uuid;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS source_channel varchar(20);
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_sale_reference
    ON transactions (tenant_id, reference_id)
    WHERE reference_type='sale' AND status='posted';

CREATE TABLE IF NOT EXISTS products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    sku varchar(40) NOT NULL,
    barcode varchar(80),
    name varchar(160) NOT NULL,
    unit varchar(30) NOT NULL DEFAULT 'pcs',
    sell_price numeric(18,2) NOT NULL DEFAULT 0 CHECK (sell_price >= 0),
    cost_price numeric(18,2) NOT NULL DEFAULT 0 CHECK (cost_price >= 0),
    stock_on_hand numeric(18,3) NOT NULL DEFAULT 0 CHECK (stock_on_hand >= 0),
    min_stock numeric(18,3) NOT NULL DEFAULT 0 CHECK (min_stock >= 0),
    is_active boolean NOT NULL DEFAULT true,
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, sku),
    UNIQUE (tenant_id, barcode)
);

CREATE TABLE IF NOT EXISTS tenant_counters (
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    counter_key varchar(40) NOT NULL,
    last_value bigint NOT NULL DEFAULT 0 CHECK (last_value >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, counter_key)
);

CREATE TABLE IF NOT EXISTS sales (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    invoice_no varchar(40) NOT NULL,
    cashier_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    channel varchar(20) NOT NULL CHECK (channel IN ('telegram','whatsapp')),
    status varchar(20) NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','void')),
    payment_method varchar(20) NOT NULL CHECK (payment_method IN ('CASH','TRANSFER','QRIS','CARD','OTHER')),
    subtotal numeric(18,2) NOT NULL CHECK (subtotal >= 0),
    discount numeric(18,2) NOT NULL DEFAULT 0 CHECK (discount >= 0),
    total numeric(18,2) NOT NULL CHECK (total >= 0),
    idempotency_key uuid NOT NULL,
    sold_at timestamptz NOT NULL DEFAULT now(),
    voided_at timestamptz,
    UNIQUE (tenant_id, invoice_no),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (total = subtotal - discount)
);

CREATE TABLE IF NOT EXISTS sale_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    sale_id uuid NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    sku_snapshot varchar(40) NOT NULL,
    product_name_snapshot varchar(160) NOT NULL,
    qty numeric(18,3) NOT NULL CHECK (qty > 0),
    unit_price numeric(18,2) NOT NULL CHECK (unit_price >= 0),
    unit_cost numeric(18,2) NOT NULL CHECK (unit_cost >= 0),
    discount numeric(18,2) NOT NULL DEFAULT 0 CHECK (discount >= 0),
    line_total numeric(18,2) NOT NULL CHECK (line_total >= 0),
    CHECK (line_total = (qty * unit_price) - discount)
);

CREATE TABLE IF NOT EXISTS sale_payments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    sale_id uuid NOT NULL REFERENCES sales(id) ON DELETE RESTRICT,
    method varchar(20) NOT NULL CHECK (method IN ('CASH','TRANSFER','QRIS','CARD','OTHER')),
    amount numeric(18,2) NOT NULL CHECK (amount > 0),
    reference varchar(120),
    status varchar(20) NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','void')),
    paid_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    movement_type varchar(24) NOT NULL
        CHECK (movement_type IN ('opening','sale','purchase','adjustment','opname','return','void')),
    qty_delta numeric(18,3) NOT NULL CHECK (qty_delta <> 0),
    stock_after numeric(18,3) NOT NULL CHECK (stock_after >= 0),
    reference_type varchar(40),
    reference_id uuid,
    note varchar(300),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stock_opname_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status varchar(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','cancelled')),
    note varchar(300),
    started_at timestamptz NOT NULL DEFAULT now(),
    posted_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opname_one_draft_per_tenant
    ON stock_opname_sessions (tenant_id) WHERE status='draft';

CREATE TABLE IF NOT EXISTS stock_opname_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    session_id uuid NOT NULL REFERENCES stock_opname_sessions(id) ON DELETE RESTRICT,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    system_qty numeric(18,3) NOT NULL CHECK (system_qty >= 0),
    counted_qty numeric(18,3) NOT NULL CHECK (counted_qty >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_products_tenant_active_name ON products (tenant_id,is_active,name);
CREATE INDEX IF NOT EXISTS idx_products_low_stock ON products (tenant_id,stock_on_hand,min_stock) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_sales_tenant_sold_at ON sales (tenant_id,sold_at DESC);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items (tenant_id,sale_id);
CREATE INDEX IF NOT EXISTS idx_stock_movements_product_time ON stock_movements (tenant_id,product_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opname_items_session ON stock_opname_items (tenant_id,session_id);

-- Every POS business table uses the same tenant context as v0.6.
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE products FORCE ROW LEVEL SECURITY;
ALTER TABLE tenant_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_counters FORCE ROW LEVEL SECURITY;
ALTER TABLE sales ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales FORCE ROW LEVEL SECURITY;
ALTER TABLE sale_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE sale_items FORCE ROW LEVEL SECURITY;
ALTER TABLE sale_payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sale_payments FORCE ROW LEVEL SECURITY;
ALTER TABLE stock_movements ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_movements FORCE ROW LEVEL SECURITY;
ALTER TABLE stock_opname_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_opname_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE stock_opname_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_opname_items FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS products_tenant_isolation ON products;
CREATE POLICY products_tenant_isolation ON products FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS tenant_counters_tenant_isolation ON tenant_counters;
CREATE POLICY tenant_counters_tenant_isolation ON tenant_counters FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS sales_tenant_isolation ON sales;
CREATE POLICY sales_tenant_isolation ON sales FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS sale_items_tenant_isolation ON sale_items;
CREATE POLICY sale_items_tenant_isolation ON sale_items FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS sale_payments_tenant_isolation ON sale_payments;
CREATE POLICY sale_payments_tenant_isolation ON sale_payments FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS stock_movements_tenant_isolation ON stock_movements;
CREATE POLICY stock_movements_tenant_isolation ON stock_movements FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS stock_opname_sessions_tenant_isolation ON stock_opname_sessions;
CREATE POLICY stock_opname_sessions_tenant_isolation ON stock_opname_sessions FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
DROP POLICY IF EXISTS stock_opname_items_tenant_isolation ON stock_opname_items;
CREATE POLICY stock_opname_items_tenant_isolation ON stock_opname_items FOR ALL
    USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);

GRANT SELECT,INSERT,UPDATE ON products,tenant_counters,sales,sale_items,
    sale_payments,stock_movements,stock_opname_sessions,stock_opname_items TO umkm_app;
GRANT SELECT,INSERT,UPDATE ON transactions,audit_logs,users,tenant_members TO umkm_app;

INSERT INTO schema_migrations(version,description)
VALUES ('007','UMKM Suite v0.7.0 POS, inventory, opname, staff RBAC')
ON CONFLICT (version) DO NOTHING;

COMMIT;
