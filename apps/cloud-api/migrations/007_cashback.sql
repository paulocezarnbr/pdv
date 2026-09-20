-- Fase 4 — cashback: saldo derivado de lançamentos, nunca sobrescrito.
CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL, phone TEXT, is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), client_uuid UUID NOT NULL,
    server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id, client_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers(tenant_id, phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS cashback_ledger (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    store_id UUID NOT NULL REFERENCES stores(id), customer_id UUID NOT NULL,
    order_id UUID NOT NULL, entry_type TEXT NOT NULL CHECK(entry_type IN ('credit','debit')),
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0), source_credit_id UUID,
    expires_at TIMESTAMPTZ, actor_user_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), client_uuid UUID NOT NULL,
    server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id, client_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cashback_credit_order
    ON cashback_ledger(tenant_id, order_id) WHERE entry_type='credit';
CREATE INDEX IF NOT EXISTS idx_cashback_customer
    ON cashback_ledger(tenant_id, customer_id, created_at);

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
ALTER TABLE cashback_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cashback_ledger FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON customers USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);
CREATE POLICY tenant_isolation ON cashback_ledger USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);
