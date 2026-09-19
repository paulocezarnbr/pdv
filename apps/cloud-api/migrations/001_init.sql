-- ===========================================================================
-- Esquema da retaguarda — ERP de food service
-- ===========================================================================
--
-- Três decisões atravessam o arquivo inteiro:
--
-- 1. **`tenant_id` em toda tabela, sempre no início da chave.** Multi-tenancy
--    por coluna, não por banco: um banco por restaurante multiplicaria o custo
--    de migração pelo número de clientes e tornaria qualquer relatório
--    consolidado impossível. O preço é que esquecer o `tenant_id` num WHERE
--    vaza dados entre clientes — por isso existe RLS no fim do arquivo, como
--    segunda barreira depois da aplicação.
--
-- 2. **`(tenant_id, client_uuid)` único em tudo que o terminal envia.** É a
--    chave de idempotência gerada no PDV. Sem ela, o reenvio — que é o caso
--    NORMAL quando o Wi-Fi da loja cai — duplicaria a venda.
--
-- 3. **`server_seq` como cursor, não `updated_at`.** Dois registros podem
--    compartilhar o mesmo timestamp, e um deles seria pulado na virada de
--    página. `server_seq` é estritamente crescente e não empata.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- Sequência global do cursor de sincronização
-- ---------------------------------------------------------------------------
-- Uma sequência para TODAS as tabelas sincronizáveis. O cursor do cliente é um
-- número só, e ele não precisa saber quantas tabelas existem para saber se
-- está em dia.
CREATE SEQUENCE IF NOT EXISTS server_seq_global;


-- ---------------------------------------------------------------------------
-- Tenants, lojas e usuários do painel
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    document    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    suspended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS stores (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    document     TEXT,
    address      TEXT,
    api_base_url TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stores_tenant ON stores (tenant_id);

-- Usuários do painel. O PIN de operação do PDV é outra coisa e vive no
-- terminal: aqui ficam as pessoas que abrem o painel administrativo.
CREATE TABLE IF NOT EXISTS panel_users (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    email                TEXT NOT NULL,
    name                 TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'manager'
                            CHECK (role IN ('owner', 'manager', 'viewer')),
    password_hash        TEXT NOT NULL,
    can_authorize        BOOLEAN NOT NULL DEFAULT FALSE,
    max_discount_percent NUMERIC(5, 2) NOT NULL DEFAULT 0,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- E-mail único POR TENANT, não globalmente: a mesma pessoa pode administrar
-- dois restaurantes diferentes, e forçar e-mails distintos a obrigaria a criar
-- um endereço por cliente.
CREATE UNIQUE INDEX IF NOT EXISTS idx_panel_users_email
    ON panel_users (tenant_id, lower(email));

CREATE TABLE IF NOT EXISTS panel_sessions (
    token_hash  TEXT PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES panel_users (id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    user_agent  TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_panel_sessions_user
    ON panel_sessions (user_id, expires_at);


-- ---------------------------------------------------------------------------
-- Terminais
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS devices (
    id            UUID PRIMARY KEY,
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    store_id      UUID NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT 'Terminal',
    -- Só o hash. Vazou o dump, os terminais continuam precisando do token cru.
    token_hash    TEXT NOT NULL,
    hostname      TEXT NOT NULL DEFAULT '',
    os            TEXT NOT NULL DEFAULT '',
    arch          TEXT NOT NULL DEFAULT '',
    activated_at  TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_token ON devices (token_hash);
CREATE INDEX IF NOT EXISTS idx_devices_tenant ON devices (tenant_id, store_id);

-- O segredo HMAC do ledger, numa tabela à parte de propósito: a consulta de
-- autenticação roda em TODA requisição e nunca deve trazer o segredo junto.
CREATE TABLE IF NOT EXISTS device_secrets (
    tenant_id  UUID NOT NULL,
    device_id  UUID NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    secret     BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, device_id)
);

CREATE TABLE IF NOT EXISTS device_activation_codes (
    code_hash   TEXT PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    store_id    UUID NOT NULL REFERENCES stores (id) ON DELETE CASCADE,
    device_id   UUID NOT NULL,
    label       TEXT NOT NULL DEFAULT 'Terminal',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at     TIMESTAMPTZ,
    used_by_ip  TEXT,
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS device_activation_attempts (
    id           BIGSERIAL PRIMARY KEY,
    ip           TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activation_attempts_ip
    ON device_activation_attempts (ip, attempted_at DESC);


-- ---------------------------------------------------------------------------
-- Cadastros (o terminal BAIXA; a retaguarda é a fonte)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    sku           TEXT NOT NULL,
    name          TEXT NOT NULL,
    barcode       TEXT,
    pricing_mode  TEXT NOT NULL DEFAULT 'unit'
                     CHECK (pricing_mode IN ('unit', 'weight')),
    price_cents   BIGINT NOT NULL DEFAULT 0,
    tare_grams    INTEGER NOT NULL DEFAULT 0,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq    BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku ON products (tenant_id, sku);
CREATE INDEX IF NOT EXISTS idx_products_cursor ON products (tenant_id, server_seq);

CREATE TABLE IF NOT EXISTS inventory_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    -- Miligramas, como no terminal: insumo em ponto flutuante acumula erro que
    -- aparece no CMV no fim do mês.
    balance_mg    BIGINT NOT NULL DEFAULT 0,
    unit_cost_cents BIGINT NOT NULL DEFAULT 0,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq    BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE INDEX IF NOT EXISTS idx_inventory_cursor
    ON inventory_items (tenant_id, server_seq);

CREATE TABLE IF NOT EXISTS recipes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    product_id  UUID NOT NULL,
    yield_grams INTEGER NOT NULL DEFAULT 1000,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq  BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE INDEX IF NOT EXISTS idx_recipes_cursor ON recipes (tenant_id, server_seq);

CREATE TABLE IF NOT EXISTS recipe_lines (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    recipe_id         UUID NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    inventory_item_id UUID NOT NULL,
    quantity_mg       BIGINT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq        BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE INDEX IF NOT EXISTS idx_recipe_lines_cursor
    ON recipe_lines (tenant_id, server_seq);

-- Réplica dos operadores do PDV. O terminal BAIXA daqui e valida o PIN
-- offline — é o que permite abrir o caixa com a internet fora.
CREATE TABLE IF NOT EXISTS users (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    login                TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'cashier',
    -- Argon2id, gerado na retaguarda e replicado. O terminal nunca envia PIN
    -- para cá; ele só recebe o hash e verifica localmente.
    pin_hash             TEXT NOT NULL,
    can_authorize        BOOLEAN NOT NULL DEFAULT FALSE,
    max_discount_percent NUMERIC(5, 2) NOT NULL DEFAULT 0,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq           BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login ON users (tenant_id, lower(login));
CREATE INDEX IF NOT EXISTS idx_users_cursor ON users (tenant_id, server_seq);


-- ---------------------------------------------------------------------------
-- Movimento (o terminal ENVIA; a nuvem é o destino inalcançável)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    id                UUID PRIMARY KEY,
    tenant_id         UUID NOT NULL,
    store_id          UUID NOT NULL,
    device_id         UUID NOT NULL,
    client_uuid       TEXT NOT NULL,
    local_number      INTEGER NOT NULL,
    channel           TEXT NOT NULL DEFAULT 'counter',
    status            TEXT NOT NULL DEFAULT 'open',
    customer_id       TEXT,
    table_id          TEXT,
    operator_id       TEXT,
    served_by_user_id TEXT,
    subtotal_cents    BIGINT NOT NULL DEFAULT 0,
    discount_cents    BIGINT NOT NULL DEFAULT 0,
    total_cents       BIGINT NOT NULL DEFAULT 0,
    -- Gorjeta FORA do total, como no terminal: somá-la ao faturamento cobraria
    -- imposto sobre dinheiro que é da equipe.
    tip_cents         BIGINT NOT NULL DEFAULT 0,
    opened_at         TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    bill_requested_at TIMESTAMPTZ,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq        BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem ON orders (tenant_id, client_uuid);
CREATE INDEX IF NOT EXISTS idx_orders_store ON orders (tenant_id, store_id, opened_at);
CREATE INDEX IF NOT EXISTS idx_orders_operator
    ON orders (tenant_id, operator_id, opened_at);

CREATE TABLE IF NOT EXISTS order_items (
    id                 UUID PRIMARY KEY,
    tenant_id          UUID NOT NULL,
    order_id           UUID NOT NULL,
    client_uuid        TEXT NOT NULL,
    product_id         TEXT,
    product_name       TEXT NOT NULL DEFAULT '',
    pricing_mode       TEXT NOT NULL DEFAULT 'unit',
    quantity           TEXT NOT NULL DEFAULT '1',
    net_weight_grams   INTEGER NOT NULL DEFAULT 0,
    unit_price_cents   BIGINT NOT NULL DEFAULT 0,
    total_cents        BIGINT NOT NULL DEFAULT 0,
    -- Prova pericial: o quadro CRU que a balança enviou. Nunca recalculado
    -- aqui — recalcular apagaria a prova e deixaria a palavra do terminal.
    scale_reading_raw  TEXT,
    canceled_at        TIMESTAMPTZ,
    canceled_by_user_id TEXT,
    cancel_reason      TEXT,
    created_by_user_id TEXT,
    created_at         TIMESTAMPTZ,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq         BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_items_idem
    ON order_items (tenant_id, client_uuid);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (tenant_id, order_id);

CREATE TABLE IF NOT EXISTS order_item_ingredients (
    id                  UUID PRIMARY KEY,
    tenant_id           UUID NOT NULL,
    order_item_id       UUID NOT NULL,
    client_uuid         TEXT NOT NULL,
    inventory_item_id   TEXT NOT NULL,
    inventory_item_name TEXT NOT NULL DEFAULT '',
    consumed_mg         BIGINT NOT NULL DEFAULT 0,
    unit_cost_cents     BIGINT NOT NULL DEFAULT 0,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq          BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oii_idem
    ON order_item_ingredients (tenant_id, client_uuid);

CREATE TABLE IF NOT EXISTS payments (
    id           UUID PRIMARY KEY,
    tenant_id    UUID NOT NULL,
    order_id     UUID NOT NULL,
    client_uuid  TEXT NOT NULL,
    method       TEXT NOT NULL,
    amount_cents BIGINT NOT NULL DEFAULT 0,
    change_cents BIGINT NOT NULL DEFAULT 0,
    nsu          TEXT,
    created_at   TIMESTAMPTZ,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq   BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idem
    ON payments (tenant_id, client_uuid);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments (tenant_id, order_id);

CREATE TABLE IF NOT EXISTS stock_movements (
    id                UUID PRIMARY KEY,
    tenant_id         UUID NOT NULL,
    store_id          UUID NOT NULL,
    client_uuid       TEXT NOT NULL,
    inventory_item_id TEXT NOT NULL,
    quantity_mg       BIGINT NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    order_item_id     TEXT,
    created_at        TIMESTAMPTZ,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq        BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_idem
    ON stock_movements (tenant_id, client_uuid);

CREATE TABLE IF NOT EXISTS cash_sessions (
    id            UUID PRIMARY KEY,
    tenant_id     UUID NOT NULL,
    store_id      UUID NOT NULL,
    device_id     UUID NOT NULL,
    client_uuid   TEXT NOT NULL,
    operator_id   TEXT,
    opened_at     TIMESTAMPTZ,
    closed_at     TIMESTAMPTZ,
    opening_cents BIGINT NOT NULL DEFAULT 0,
    closing_cents BIGINT NOT NULL DEFAULT 0,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq    BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_idem
    ON cash_sessions (tenant_id, client_uuid);


-- ---------------------------------------------------------------------------
-- Auditoria — o destino inalcançável
-- ---------------------------------------------------------------------------
--
-- Esta é a tabela que dá sentido ao sistema inteiro. Quem controla o PC da
-- loja controla o banco local; o que já chegou aqui, não.

CREATE TABLE IF NOT EXISTS audit_ledger (
    id                 UUID PRIMARY KEY,
    tenant_id          UUID NOT NULL,
    store_id           UUID NOT NULL,
    device_id          UUID NOT NULL,
    client_uuid        TEXT NOT NULL,
    seq                BIGINT NOT NULL,
    event_type         TEXT NOT NULL,
    severity           TEXT NOT NULL DEFAULT 'info',
    actor_user_id      TEXT,
    authorizer_user_id TEXT,
    payload_json       TEXT NOT NULL,
    prev_hash          TEXT NOT NULL,
    hash               TEXT NOT NULL,
    created_at         TIMESTAMPTZ,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    server_seq         BIGINT NOT NULL DEFAULT nextval('server_seq_global')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_idem
    ON audit_ledger (tenant_id, client_uuid);
-- Sequência sem buraco POR DEVICE. É o índice que torna barata a checagem da
-- marca d'água alta, feita em todo item de auditoria que chega.
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_device_seq
    ON audit_ledger (tenant_id, device_id, seq);

-- Marca d'água alta: uma vez ancorado o seq N, reenviar o seq N com conteúdo
-- diferente é rejeitado e vira alerta. É isto que torna a venda sincronizada
-- inalcançável para quem controla o PC da loja.
CREATE TABLE IF NOT EXISTS device_anchors (
    tenant_id  UUID NOT NULL,
    device_id  UUID NOT NULL,
    last_seq   BIGINT NOT NULL,
    last_hash  TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, device_id)
);

CREATE TABLE IF NOT EXISTS fraud_alerts (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    store_id    UUID,
    device_id   UUID NOT NULL,
    reason      TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    raised_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_by UUID
);
CREATE INDEX IF NOT EXISTS idx_fraud_open
    ON fraud_alerts (tenant_id, resolved_at, raised_at DESC);


-- ---------------------------------------------------------------------------
-- Idempotência do lote e comandos do painel
-- ---------------------------------------------------------------------------

-- `Idempotency-Key` cobre o LOTE; `client_uuid` cobre cada ITEM. A redundância
-- é proposital: a chave do lote evita reprocessamento custoso, e a do item
-- garante a correção mesmo se o lote for remontado de outra forma.
CREATE TABLE IF NOT EXISTS sync_batches (
    idempotency_key TEXT NOT NULL,
    tenant_id       UUID NOT NULL,
    device_id       UUID NOT NULL,
    response_json   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, device_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_sync_batches_age ON sync_batches (created_at);

CREATE TABLE IF NOT EXISTS remote_commands (
    command_uuid      TEXT PRIMARY KEY,
    tenant_id         UUID NOT NULL,
    store_id          UUID NOT NULL,
    device_id         UUID NOT NULL,
    kind              TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    issued_by_user_id UUID NOT NULL,
    issued_by_name    TEXT NOT NULL DEFAULT '',
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    signature         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'applied', 'refused')),
    delivered_at      TIMESTAMPTZ,
    delivery_count    INTEGER NOT NULL DEFAULT 0,
    settled_at        TIMESTAMPTZ,
    reported_at       TIMESTAMPTZ,
    result_message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_pending
    ON remote_commands (tenant_id, device_id, status, issued_at);
CREATE INDEX IF NOT EXISTS idx_commands_operator
    ON remote_commands (tenant_id, issued_by_user_id, issued_at DESC);


-- ---------------------------------------------------------------------------
-- RLS — a segunda barreira
-- ---------------------------------------------------------------------------
--
-- A aplicação já filtra por `tenant_id` em toda consulta. O RLS existe porque
-- "já filtra" depende de ninguém esquecer, e esquecer um WHERE numa consulta
-- nova é o erro mais comum que existe — e o que vaza faturamento de um cliente
-- para outro.
--
-- O `app.tenant_id` é definido por `SET LOCAL` dentro da transação, então ele
-- vale para aquela transação e some junto com ela. Uma conexão reaproveitada
-- do pool nunca herda o tenant da requisição anterior.

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'orders', 'order_items', 'order_item_ingredients', 'payments',
        'stock_movements', 'cash_sessions', 'audit_ledger', 'remote_commands',
        'fraud_alerts', 'products', 'inventory_items', 'recipes',
        'recipe_lines', 'users'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING ('
            '  current_setting(''app.tenant_id'', true) IS NULL'
            '  OR tenant_id::text = current_setting(''app.tenant_id'', true)'
            ')', t);
    END LOOP;
END
$$;
