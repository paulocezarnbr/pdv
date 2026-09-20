CREATE TABLE IF NOT EXISTS prepaid_ledger (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    store_id UUID NOT NULL REFERENCES stores(id), customer_id UUID NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('deposit','debit','refund')),
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0), order_id UUID,
    actor_user_id UUID NOT NULL, authorizer_user_id UUID,
    created_at TIMESTAMPTZ NOT NULL, client_uuid UUID NOT NULL,
    server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id, client_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prepaid_debit_order
    ON prepaid_ledger(tenant_id, order_id) WHERE entry_type='debit';
CREATE INDEX IF NOT EXISTS idx_prepaid_customer
    ON prepaid_ledger(tenant_id, customer_id, created_at);
ALTER TABLE prepaid_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE prepaid_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON prepaid_ledger USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);
