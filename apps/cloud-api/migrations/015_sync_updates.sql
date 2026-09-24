-- Atualizações vindas do terminal: registro de idempotência.
--
-- Até aqui a nuvem só sabia INSERIR. Toda operação `update` do outbox — pedir
-- a conta, trocar a mesa, receber a comanda, cancelar — virava um INSERT com o
-- mesmo `id` do pedido, batia no NOT NULL de `local_number` (e, se passasse,
-- na chave primária) e derrubava o lote inteiro. O terminal reenviava até
-- desistir, e a nuvem nunca via a mesa fechada.
--
-- Um INSERT se deduplica pelo próprio `client_uuid` da linha. Um UPDATE não
-- deixa rastro na linha que altera, então o rastro mora aqui: o `client_uuid`
-- da OPERAÇÃO. Sem isto, um lote reenviado depois de uma resposta perdida
-- reaplicaria uma mudança velha por cima de uma nova — trocar a mesa de volta,
-- por exemplo.
CREATE TABLE IF NOT EXISTS sync_applied_updates (
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    client_uuid  TEXT NOT NULL,
    device_id    UUID NOT NULL,
    entity_table TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, client_uuid)
);
CREATE INDEX IF NOT EXISTS idx_sync_applied_updates_entity
    ON sync_applied_updates (tenant_id, entity_table, entity_id);

ALTER TABLE sync_applied_updates ENABLE ROW LEVEL SECURITY;
ALTER TABLE sync_applied_updates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON sync_applied_updates;
CREATE POLICY tenant_isolation ON sync_applied_updates USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);

-- O registro só cresce: apagar uma linha reabriria a porta do reenvio.
REVOKE UPDATE, DELETE ON sync_applied_updates FROM erp_app;
GRANT SELECT, INSERT ON sync_applied_updates TO erp_app;

-- O mapa do salão. O terminal já o enviava (`edge/tables.py`), e a nuvem não
-- tinha onde guardar: cada mesa criada ou renomeada ia para a quarentena do
-- terminal, para sempre — e uma quarentena sempre cheia é uma quarentena que
-- ninguém mais olha, inclusive quando aparece nela uma venda de verdade.
CREATE TABLE IF NOT EXISTS store_tables (
    id          UUID PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    store_id    UUID NOT NULL REFERENCES stores(id),
    label       TEXT NOT NULL,
    area        TEXT NOT NULL DEFAULT 'Salão',
    seats       INTEGER NOT NULL DEFAULT 4,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_uuid TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq  BIGINT NOT NULL DEFAULT nextval('server_seq_global'),
    UNIQUE (tenant_id, client_uuid)
);
CREATE INDEX IF NOT EXISTS idx_store_tables_store ON store_tables (tenant_id, store_id);

ALTER TABLE store_tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE store_tables FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON store_tables;
CREATE POLICY tenant_isolation ON store_tables USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);
GRANT SELECT, INSERT, UPDATE ON store_tables TO erp_app;
