-- ===========================================================================
-- Fase 6 — Cardápio QR com upsell contextual
-- ===========================================================================
--
-- O cardápio é a PRIMEIRA página pública da retaguarda: quem abre é o cliente
-- na mesa, sem login. Por isso ela não lê nada pelo tenant de uma sessão — ela
-- resolve o link (`menu_links.token`) e só então entra no tenant da loja.

-- O que o cardápio mostra de cada produto. `category` também desce para o
-- caixa (ver `pdv/sync/pull_mapping.py`); descrição e visibilidade são só do
-- cardápio. Visível por padrão: um cardápio vazio no primeiro dia faria o dono
-- achar que a função não funciona.
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS category TEXT,
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS menu_visible BOOLEAN NOT NULL DEFAULT TRUE;

-- Um link por loja, ou por mesa. O token vai impresso no QR e é público por
-- definição — não é credencial. Ele é aleatório para que os cardápios não sejam
-- enumeráveis (trocar um número na URL não abre o de outro restaurante), e
-- revogável para o dia em que um QR impresso precisar sair de circulação.
-- Guardado em claro porque o painel precisa reimprimir o mesmo QR.
CREATE TABLE IF NOT EXISTS menu_links (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    store_id    UUID NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE CHECK (length(token) >= 20),
    table_label TEXT CHECK (table_label IS NULL OR length(table_label) <= 40),
    created_by  UUID REFERENCES panel_users (id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_menu_links_store ON menu_links (tenant_id, store_id);

ALTER TABLE menu_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE menu_links FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON menu_links;
CREATE POLICY tenant_isolation ON menu_links USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);

-- Coocorrência de itens por pedido, o insumo do upsell. O índice atende a
-- junção do pedido consigo mesmo sem varrer a tabela inteira de itens.
CREATE INDEX IF NOT EXISTS idx_order_items_product
    ON order_items (tenant_id, order_id, product_id);
