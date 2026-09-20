CREATE TABLE IF NOT EXISTS discount_tiers (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    store_id UUID NOT NULL REFERENCES stores(id), code TEXT NOT NULL, name TEXT NOT NULL,
    percent_basis_points INTEGER NOT NULL CHECK(percent_basis_points BETWEEN 0 AND 10000),
    priority INTEGER NOT NULL DEFAULT 0, requires_manager BOOLEAN NOT NULL DEFAULT FALSE,
    valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL,
    client_uuid UUID NOT NULL, server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id,client_uuid), UNIQUE(tenant_id,store_id,code)
);
CREATE TABLE IF NOT EXISTS customer_discount_tiers (
    customer_id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    tier_id UUID NOT NULL, assigned_by_user_id UUID NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL, client_uuid UUID NOT NULL,
    server_seq BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE(tenant_id,client_uuid)
);
ALTER TABLE discount_tiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE discount_tiers FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_discount_tiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_discount_tiers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON discount_tiers USING (
    coalesce(current_setting('app.tenant_id',true),'')='' OR
    tenant_id::text=current_setting('app.tenant_id',true));
CREATE POLICY tenant_isolation ON customer_discount_tiers USING (
    coalesce(current_setting('app.tenant_id',true),'')='' OR
    tenant_id::text=current_setting('app.tenant_id',true));
