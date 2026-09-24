-- ===========================================================================
-- Fase 6 — previsão de demanda: a contagem de estoque que dá saldo à sugestão
-- ===========================================================================
--
-- A sugestão de compra precisa de saldo, e a nuvem não tinha nenhum: o caixa
-- não registra compra nem contagem, só baixa por venda. Somar só as baixas dá
-- um saldo negativo que não é de ninguém, e sugerir compra contra ele faria
-- comprar o que já está na prateleira.
--
-- A contagem é feita por quem está na loja e lançada no painel. O saldo passa a
-- ser a última contagem MAIS os movimentos sincronizados depois dela — vendas
-- baixam, estornos devolvem. Sem contagem, o painel mostra o consumo previsto e
-- pede a contagem, em vez de inventar uma sugestão.
CREATE TABLE IF NOT EXISTS inventory_counts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    store_id          UUID NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    -- O id do insumo no CAIXA (texto, como em `stock_movements`): é por ele que
    -- as baixas chegam. O nome vai junto porque o cadastro de insumo não sobe.
    inventory_item_id TEXT NOT NULL,
    inventory_item_name TEXT NOT NULL,
    counted_mg        BIGINT NOT NULL CHECK (counted_mg >= 0),
    counted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    counted_by        UUID REFERENCES panel_users (id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_counts_item
    ON inventory_counts (tenant_id, store_id, inventory_item_id, counted_at DESC);

ALTER TABLE inventory_counts ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_counts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON inventory_counts;
CREATE POLICY tenant_isolation ON inventory_counts USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);

-- As séries diárias por loja saem daqui. Sem o índice, a previsão varre o
-- histórico inteiro de todas as lojas do restaurante a cada cálculo.
CREATE INDEX IF NOT EXISTS idx_orders_store_closed
    ON orders (tenant_id, store_id, closed_at) WHERE status = 'paid';
CREATE INDEX IF NOT EXISTS idx_stock_movements_item
    ON stock_movements (tenant_id, store_id, inventory_item_id, created_at);
