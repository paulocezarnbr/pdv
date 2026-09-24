-- ===========================================================================
-- Contrato do push: a nuvem aceita o que o caixa realmente manda
-- ===========================================================================
--
-- Até aqui a primeira venda de balcão com receita abortava o lote inteiro
-- (`stock_movements.quantity_mg` nulo), e nada do caixa chegava. Os dois lados
-- eram testados cada um contra o próprio dublê. A correção mora no merger
-- (`src/lib/sync/merge.ts`); este arquivo só dá a ele onde gravar.

-- A venda de balcão dos caixas até a 1.1.2 não mandava o número local. Aqui ele
-- era obrigatório, e a obrigação derrubava o lote. Nulo quer dizer "caixa
-- antigo não informou" — inventar um zero diria que a venda teve número zero.
ALTER TABLE orders ALTER COLUMN local_number DROP NOT NULL;

-- O cadastro de mesas do salão. O caixa sempre o enviou e a nuvem recusava por
-- não conhecer a tabela: cada mesa criada virava um item em quarentena no
-- terminal. Chave é o `id` do caixa — é por ele que `orders.table_id` aponta.
CREATE TABLE IF NOT EXISTS store_tables (
    id          UUID PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    store_id    UUID NOT NULL,
    client_uuid TEXT NOT NULL,
    label       TEXT NOT NULL,
    area        TEXT,
    seats       INTEGER,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq  BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_store_tables_idem
    ON store_tables (tenant_id, client_uuid);
CREATE INDEX IF NOT EXISTS idx_store_tables_store
    ON store_tables (tenant_id, store_id, sort_order);

ALTER TABLE store_tables ENABLE ROW LEVEL SECURITY;
ALTER TABLE store_tables FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON store_tables;
CREATE POLICY tenant_isolation ON store_tables USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);

-- A baixa de insumo por item vendido, agora que ela chega. É a consulta do CMV
-- e da previsão de consumo.
CREATE INDEX IF NOT EXISTS idx_oii_item
    ON order_item_ingredients (tenant_id, order_item_id);

-- O gatilho de proteção dos níveis Funcionário e Dono (011) terminava com
-- `RETURN NEW` também no DELETE. Num gatilho BEFORE DELETE o `NEW` é nulo, e
-- devolver nulo CANCELA a operação em silêncio: nenhum vínculo de nível, de
-- nível nenhum, jamais foi excluído — e o restaurante que tivesse um cliente
-- classificado não podia mais ser excluído (a FK para `tenants` segurava).
-- A proteção continua igual; só a exclusão permitida passa a acontecer.
CREATE OR REPLACE FUNCTION prevent_protected_discount_tier_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM discount_tiers t
        WHERE t.id = OLD.tier_id
          AND t.tenant_id = OLD.tenant_id
          AND t.code IN ('employee', 'owner')
    ) THEN
        RAISE EXCEPTION 'protected discount tier cannot be removed'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.tier_id IS DISTINCT FROM NEW.tier_id AND EXISTS (
        SELECT 1
        FROM discount_tiers t
        WHERE t.id = OLD.tier_id
          AND t.tenant_id = OLD.tenant_id
          AND t.code IN ('employee', 'owner')
    ) THEN
        RAISE EXCEPTION 'protected discount tier cannot be changed'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
