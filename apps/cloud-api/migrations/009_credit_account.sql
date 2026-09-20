CREATE TABLE IF NOT EXISTS customer_credit_accounts (
    customer_id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    limit_cents BIGINT NOT NULL CHECK(limit_cents >= 0),
    due_days INTEGER NOT NULL CHECK(due_days BETWEEN 1 AND 365),
    is_active BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL,
    client_uuid UUID NOT NULL, server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id,client_uuid)
);
CREATE TABLE IF NOT EXISTS credit_account_ledger (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    store_id UUID NOT NULL REFERENCES stores(id), customer_id UUID NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('charge','payment','forgive')),
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0), order_id UUID,
    source_charge_id UUID, due_at TIMESTAMPTZ, actor_user_id UUID NOT NULL,
    authorizer_user_id UUID, created_at TIMESTAMPTZ NOT NULL, client_uuid UUID NOT NULL,
    server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id,client_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_charge_order
    ON credit_account_ledger(tenant_id,order_id) WHERE entry_type='charge';
CREATE INDEX IF NOT EXISTS idx_credit_customer_due
    ON credit_account_ledger(tenant_id,customer_id,due_at,created_at);
ALTER TABLE customer_credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_credit_accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE credit_account_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_account_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customer_credit_accounts USING (
    coalesce(current_setting('app.tenant_id',true),'')='' OR
    tenant_id::text=current_setting('app.tenant_id',true));
CREATE POLICY tenant_isolation ON credit_account_ledger USING (
    coalesce(current_setting('app.tenant_id',true),'')='' OR
    tenant_id::text=current_setting('app.tenant_id',true));
