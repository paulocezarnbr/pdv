-- Emissão fiscal server-first. A nuvem é a autoridade da série normal;
-- cada PDV conserva uma série diferente apenas para contingência offline.
CREATE TABLE IF NOT EXISTS fiscal_configurations (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'pynfe' CHECK (provider IN ('pynfe')),
    uf CHAR(2) NOT NULL DEFAULT 'RJ',
    environment TEXT NOT NULL DEFAULT 'homologation'
        CHECK (environment IN ('homologation', 'production')),
    certificate_ref TEXT,
    csc_ref TEXT,
    csc_id TEXT,
    cnpj TEXT,
    state_registration TEXT,
    tax_regime SMALLINT,
    legal_name TEXT,
    address_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, store_id)
);

CREATE TABLE IF NOT EXISTS fiscal_series (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    device_id UUID REFERENCES devices(id) ON DELETE RESTRICT,
    model SMALLINT NOT NULL DEFAULT 65 CHECK (model IN (59, 65)),
    series INTEGER NOT NULL CHECK (series BETWEEN 1 AND 999),
    purpose TEXT NOT NULL CHECK (purpose IN ('normal', 'offline_contingency')),
    next_number BIGINT NOT NULL DEFAULT 1 CHECK (next_number > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, store_id, model, series),
    -- Uma série de contingência por terminal e modelo.
    UNIQUE (tenant_id, store_id, device_id, model, purpose),
    CHECK (
      (purpose = 'normal' AND device_id IS NULL) OR
      (purpose = 'offline_contingency' AND device_id IS NOT NULL)
    )
);
-- Uma série NORMAL por loja e modelo. A série normal não tem terminal
-- (`device_id IS NULL`), e num UNIQUE comum dois NULL não colidem — duas
-- séries normais passariam. Índice parcial em vez de `UNIQUE NULLS NOT
-- DISTINCT`: aquela sintaxe só existe a partir do PostgreSQL 15, e num banco
-- 14 ela derrubava esta migration inteira — e com ela o contêiner na subida,
-- que o Coolify mostra apenas como "unhealthy".
CREATE UNIQUE INDEX IF NOT EXISTS uq_fiscal_series_normal_per_store
    ON fiscal_series (tenant_id, store_id, model, purpose)
    WHERE device_id IS NULL;

CREATE TABLE IF NOT EXISTS fiscal_product_profiles (
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    product_id TEXT NOT NULL,
    ncm CHAR(8) NOT NULL,
    cfop CHAR(4) NOT NULL,
    cest CHAR(7),
    unit_code TEXT NOT NULL DEFAULT 'UN',
    origin SMALLINT NOT NULL DEFAULT 0 CHECK (origin BETWEEN 0 AND 8),
    csosn CHAR(3),
    cst_icms CHAR(2),
    cst_pis CHAR(2) NOT NULL DEFAULT '49',
    cst_cofins CHAR(2) NOT NULL DEFAULT '49',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, product_id),
    CHECK ((csosn IS NOT NULL) <> (cst_icms IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS fiscal_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    order_id UUID NOT NULL,
    request_uuid UUID NOT NULL,
    model SMALLINT NOT NULL DEFAULT 65 CHECK (model IN (59, 65)),
    series INTEGER NOT NULL CHECK (series BETWEEN 1 AND 999),
    number BIGINT NOT NULL CHECK (number > 0),
    emission_type TEXT NOT NULL DEFAULT 'normal'
        CHECK (emission_type IN ('normal', 'offline_contingency')),
    status TEXT NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'unknown', 'authorized', 'rejected',
                          'canceled', 'contingency_pending')),
    access_key TEXT,
    protocol TEXT,
    xml_content TEXT,
    provider_code TEXT,
    provider_reason TEXT,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    authorized_at TIMESTAMPTZ,
    UNIQUE (tenant_id, request_uuid),
    UNIQUE (tenant_id, store_id, model, series, number)
);
CREATE INDEX IF NOT EXISTS idx_fiscal_documents_order
    ON fiscal_documents(tenant_id, order_id);
CREATE INDEX IF NOT EXISTS idx_fiscal_documents_unsettled
    ON fiscal_documents(tenant_id, store_id, status)
    WHERE status IN ('processing', 'unknown', 'contingency_pending');

CREATE TABLE IF NOT EXISTS fiscal_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    fiscal_document_id UUID NOT NULL REFERENCES fiscal_documents(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fiscal_events_document
    ON fiscal_events(tenant_id, fiscal_document_id, created_at);

ALTER TABLE fiscal_configurations ENABLE ROW LEVEL SECURITY;
ALTER TABLE fiscal_configurations FORCE ROW LEVEL SECURITY;
ALTER TABLE fiscal_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE fiscal_series FORCE ROW LEVEL SECURITY;
ALTER TABLE fiscal_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE fiscal_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE fiscal_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE fiscal_events FORCE ROW LEVEL SECURITY;
ALTER TABLE fiscal_product_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE fiscal_product_profiles FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON fiscal_configurations USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON fiscal_series USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON fiscal_documents USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON fiscal_events USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON fiscal_product_profiles USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true));

CREATE OR REPLACE FUNCTION prevent_fiscal_event_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'fiscal events are immutable' USING ERRCODE='42501';
END;
$$;
CREATE TRIGGER trg_fiscal_events_no_update
BEFORE UPDATE ON fiscal_events FOR EACH ROW EXECUTE FUNCTION prevent_fiscal_event_mutation();
CREATE TRIGGER trg_fiscal_events_no_delete
BEFORE DELETE ON fiscal_events FOR EACH ROW EXECUTE FUNCTION prevent_fiscal_event_mutation();

-- O papel de execução recebe DML, não DDL nem remoção do histórico fiscal.
GRANT SELECT, INSERT, UPDATE ON fiscal_configurations, fiscal_series,
    fiscal_product_profiles, fiscal_documents TO erp_app;
GRANT SELECT, INSERT ON fiscal_events TO erp_app;
REVOKE DELETE ON fiscal_configurations, fiscal_series, fiscal_product_profiles,
    fiscal_documents, fiscal_events FROM erp_app;
REVOKE UPDATE ON fiscal_events FROM erp_app;
