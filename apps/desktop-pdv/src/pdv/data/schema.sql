-- ===========================================================================
-- Schema local do PDV (SQLite) — espelho offline de docs/der.md
--
-- Convenções:
--   * Dinheiro  : INTEGER em centavos      (nunca REAL)
--   * Peso      : INTEGER em gramas
--   * Insumo    : INTEGER em miligramas/ml (permite baixa fracionada exata)
--   * Datas     : TEXT ISO-8601 em UTC
--   * is_synced : 0 = pendente, 1 = confirmado pelo servidor (com ACK)
-- ===========================================================================

-- --------------------------------------------------------------------------
-- Catálogo
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    store_id          TEXT NOT NULL,
    sku               TEXT NOT NULL,
    barcode           TEXT,
    name              TEXT NOT NULL,
    category          TEXT,
    pricing_mode      TEXT NOT NULL CHECK (pricing_mode IN ('unit', 'weight')),
    price_cents       INTEGER NOT NULL CHECK (price_cents >= 0), -- se weight: por kg
    tare_grams        INTEGER NOT NULL DEFAULT 0,
    recipe_id         TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    -- sync (cadastro mutável → LWW)
    updated_at        TEXT NOT NULL,
    sync_version      INTEGER NOT NULL DEFAULT 0,
    server_seq        INTEGER,
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_tenant ON products (tenant_id, is_active);
CREATE INDEX IF NOT EXISTS idx_products_barcode ON products (barcode);

CREATE TABLE IF NOT EXISTS recipes (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    base_qty_g    INTEGER NOT NULL CHECK (base_qty_g > 0),
    yield_factor  TEXT NOT NULL DEFAULT '1.0',   -- Decimal como TEXT: sem float
    updated_at    TEXT NOT NULL,
    sync_version  INTEGER NOT NULL DEFAULT 0,
    server_seq    INTEGER,
    FOREIGN KEY (product_id) REFERENCES products (id)
);

CREATE TABLE IF NOT EXISTS recipe_lines (
    id                 TEXT PRIMARY KEY,
    recipe_id          TEXT NOT NULL,
    inventory_item_id  TEXT NOT NULL,
    qty_per_base_mg    INTEGER NOT NULL CHECK (qty_per_base_mg >= 0),
    waste_percent      TEXT NOT NULL DEFAULT '0',
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (recipe_id) REFERENCES recipes (id),
    FOREIGN KEY (inventory_item_id) REFERENCES inventory_items (id)
);
CREATE INDEX IF NOT EXISTS idx_recipe_lines_recipe ON recipe_lines (recipe_id);

-- --------------------------------------------------------------------------
-- Estoque
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_items (
    id                      TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    store_id                TEXT NOT NULL,
    name                    TEXT NOT NULL,
    unit                    TEXT NOT NULL CHECK (unit IN ('mg', 'ml', 'un')),
    -- Cache do saldo. A VERDADE é a soma de stock_movements; esta coluna existe
    -- só para consulta rápida e é recalculável a qualquer momento.
    balance_mg              INTEGER NOT NULL DEFAULT 0,
    min_stock_mg            INTEGER NOT NULL DEFAULT 0,
    avg_cost_cents_per_kg   INTEGER NOT NULL DEFAULT 0,
    updated_at              TEXT NOT NULL,
    sync_version            INTEGER NOT NULL DEFAULT 0,
    server_seq              INTEGER
);

-- Append-only: o saldo é derivado daqui. Nunca sincronizamos saldo absoluto,
-- apenas movimentos — o que torna a fusão de duas lojas offline trivial.
CREATE TABLE IF NOT EXISTS stock_movements (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    store_id           TEXT NOT NULL,
    inventory_item_id  TEXT NOT NULL,
    qty_mg             INTEGER NOT NULL,          -- negativo = saída
    movement_type      TEXT NOT NULL CHECK (
                          movement_type IN ('sale','purchase','waste','adjustment','inventory')),
    reference_type     TEXT,
    reference_id       TEXT,
    unit_cost_cents    INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    origin_device_id   TEXT NOT NULL,
    -- sync (append-only → só deduplicação)
    client_uuid        TEXT NOT NULL UNIQUE,
    is_synced          INTEGER NOT NULL DEFAULT 0,
    synced_at          TEXT,
    FOREIGN KEY (inventory_item_id) REFERENCES inventory_items (id)
);
CREATE INDEX IF NOT EXISTS idx_stock_mov_pending ON stock_movements (is_synced, created_at);
CREATE INDEX IF NOT EXISTS idx_stock_mov_item ON stock_movements (inventory_item_id);

-- --------------------------------------------------------------------------
-- Vendas
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT NOT NULL,
    store_id               TEXT NOT NULL,
    device_id              TEXT NOT NULL,
    local_number           INTEGER NOT NULL,      -- sequência POR DEVICE
    channel                TEXT NOT NULL DEFAULT 'counter',
    status                 TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','paid','canceled')),
    customer_id            TEXT,
    operator_id            TEXT NOT NULL,
    subtotal_cents         INTEGER NOT NULL DEFAULT 0,
    discount_cents         INTEGER NOT NULL DEFAULT 0,
    total_cents            INTEGER NOT NULL DEFAULT 0,
    discount_tier_id       TEXT,
    authorized_by_user_id  TEXT,
    opened_at              TEXT NOT NULL,
    closed_at              TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    origin_device_id       TEXT NOT NULL,
    client_uuid            TEXT NOT NULL UNIQUE,
    is_synced              INTEGER NOT NULL DEFAULT 0,
    synced_at              TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_local_number
    ON orders (device_id, local_number);
CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders (is_synced, created_at);

CREATE TABLE IF NOT EXISTS order_items (
    id                    TEXT PRIMARY KEY,
    order_id              TEXT NOT NULL,
    tenant_id             TEXT NOT NULL,
    product_id            TEXT NOT NULL,
    product_name          TEXT NOT NULL,          -- snapshot: o nome pode mudar
    pricing_mode          TEXT NOT NULL,
    quantity              TEXT NOT NULL DEFAULT '1',
    gross_weight_grams    INTEGER NOT NULL DEFAULT 0,
    tare_grams            INTEGER NOT NULL DEFAULT 0,
    net_weight_grams      INTEGER NOT NULL DEFAULT 0,
    unit_price_cents      INTEGER NOT NULL,
    total_cents           INTEGER NOT NULL,
    -- Prova pericial: o quadro CRU que a balança enviou.
    scale_reading_raw     TEXT,
    canceled_at           TEXT,
    canceled_by_user_id   TEXT,
    cancel_reason         TEXT,
    created_at            TEXT NOT NULL,
    client_uuid           TEXT NOT NULL UNIQUE,
    is_synced             INTEGER NOT NULL DEFAULT 0,
    synced_at             TEXT,
    FOREIGN KEY (order_id) REFERENCES orders (id)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);

-- Foto imutável da baixa: a ficha técnica muda com o tempo, o que saiu do
-- estoque naquele dia não.
CREATE TABLE IF NOT EXISTS order_item_ingredients (
    id                 TEXT PRIMARY KEY,
    order_item_id      TEXT NOT NULL,
    inventory_item_id  TEXT NOT NULL,
    inventory_item_name TEXT NOT NULL,
    consumed_mg        INTEGER NOT NULL,
    unit_cost_cents    INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    client_uuid        TEXT NOT NULL UNIQUE,
    is_synced          INTEGER NOT NULL DEFAULT 0,
    synced_at          TEXT,
    FOREIGN KEY (order_item_id) REFERENCES order_items (id)
);

CREATE TABLE IF NOT EXISTS payments (
    id             TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    method         TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    change_cents   INTEGER NOT NULL DEFAULT 0,
    nsu            TEXT,
    created_at     TEXT NOT NULL,
    client_uuid    TEXT NOT NULL UNIQUE,
    is_synced      INTEGER NOT NULL DEFAULT 0,
    synced_at      TEXT,
    FOREIGN KEY (order_id) REFERENCES orders (id)
);

-- --------------------------------------------------------------------------
-- Auditoria anti-furto — imutável, encadeada por hash
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_ledger (
    id                    TEXT PRIMARY KEY,
    tenant_id             TEXT NOT NULL,
    store_id              TEXT NOT NULL,
    device_id             TEXT NOT NULL,
    -- Sequência sem buraco POR DEVICE. Um gap = adulteração do arquivo local.
    seq                   INTEGER NOT NULL,
    event_type            TEXT NOT NULL,
    severity              TEXT NOT NULL DEFAULT 'info',
    actor_user_id         TEXT NOT NULL,
    authorizer_user_id    TEXT,
    payload_json          TEXT NOT NULL,          -- JSON canônico (chaves ordenadas)
    prev_hash             TEXT NOT NULL,
    hash                  TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    client_uuid           TEXT NOT NULL UNIQUE,
    is_synced             INTEGER NOT NULL DEFAULT 0,
    synced_at             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_device_seq ON audit_ledger (device_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_pending ON audit_ledger (is_synced, seq);

-- Impede UPDATE/DELETE no ledger a partir da aplicação. Não detém quem abrir o
-- arquivo com um editor de SQLite — por isso a defesa real é a hash chain
-- revalidada no servidor. Este gatilho pega o erro honesto de programação.
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_ledger
FOR EACH ROW WHEN OLD.hash <> NEW.hash OR OLD.payload_json <> NEW.payload_json
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger e imutavel');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_ledger
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger e imutavel');
END;

-- --------------------------------------------------------------------------
-- Caixa
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cash_sessions (
    id                      TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    store_id                TEXT NOT NULL,
    device_id               TEXT NOT NULL,
    user_id                 TEXT NOT NULL,
    opened_at               TEXT NOT NULL,
    closed_at               TEXT,
    opening_amount_cents    INTEGER NOT NULL DEFAULT 0,
    -- Conciliação CEGA: declared_* é gravado ANTES de expected_* existir.
    declared_amount_cents   INTEGER,
    expected_amount_cents   INTEGER,
    difference_cents        INTEGER,
    blind_close             INTEGER NOT NULL DEFAULT 1,
    client_uuid             TEXT NOT NULL UNIQUE,
    is_synced               INTEGER NOT NULL DEFAULT 0,
    synced_at               TEXT
);

-- --------------------------------------------------------------------------
-- Sincronização (tabelas LOCAIS — nunca sobem como entidade)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_outbox (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,  -- ordem de envio
    entity_table   TEXT NOT NULL,
    entity_id      TEXT NOT NULL,
    client_uuid    TEXT NOT NULL,
    operation      TEXT NOT NULL CHECK (operation IN ('insert','update','delete')),
    payload_json   TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    available_at   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_ready ON sync_outbox (available_at, seq);

CREATE TABLE IF NOT EXISTS sync_cursors (
    entity_table     TEXT PRIMARY KEY,
    last_server_seq  INTEGER NOT NULL DEFAULT 0,
    last_pulled_at   TEXT
);

CREATE TABLE IF NOT EXISTS device_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Sequências locais (número da venda por terminal, seq do ledger)
CREATE TABLE IF NOT EXISTS local_counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL DEFAULT 0
);

-- Usuários replicados para autorização OFFLINE (senha de gerente sem internet)
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    login          TEXT NOT NULL,
    role           TEXT NOT NULL,
    password_hash  TEXT,       -- Argon2id
    pin_hash       TEXT,       -- Argon2id
    max_discount_percent TEXT NOT NULL DEFAULT '0',
    can_authorize  INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT NOT NULL
);

-- ===========================================================================
-- Fase 3 — Servidor local (edge) para o app do garçom e o KDS
-- ===========================================================================

-- Aparelhos de garçom pareados com ESTE terminal.
--
-- A LAN da loja não é confiável: costuma ser a mesma rede do Wi-Fi do cliente.
-- Estar na rede não autoriza nada — o aparelho precisa ter sido pareado por
-- alguém com acesso físico ao caixa, e carrega um token próprio.
CREATE TABLE IF NOT EXISTS edge_devices (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    name          TEXT NOT NULL,           -- "Celular da Ana"
    kind          TEXT NOT NULL DEFAULT 'waiter'
                     CHECK (kind IN ('waiter','kds')),
    token_hash    TEXT NOT NULL,           -- SHA-256; o token cru nunca é gravado
    operator_id   TEXT,                    -- garçom vinculado, quando houver
    paired_at     TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,                    -- celular perdido se revoga daqui
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    client_uuid   TEXT NOT NULL UNIQUE,
    is_synced     INTEGER NOT NULL DEFAULT 0,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_edge_devices_token ON edge_devices (token_hash);

-- Códigos de pareamento de uso único, exibidos na tela do caixa.
CREATE TABLE IF NOT EXISTS edge_pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    used_by     TEXT
);

-- Fila do KDS. Espelha os itens do pedido, mas com o ciclo de vida da COZINHA,
-- que é diferente do ciclo de vida da venda: um item pago pode ainda não ter
-- saído, e um item entregue pode ser devolvido.
CREATE TABLE IF NOT EXISTS kds_tickets (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    store_id       TEXT NOT NULL,
    order_id       TEXT NOT NULL,
    order_item_id  TEXT NOT NULL,
    station        TEXT NOT NULL DEFAULT 'cozinha',
    product_name   TEXT NOT NULL,
    quantity       TEXT NOT NULL DEFAULT '1',
    notes          TEXT,
    status         TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued','preparing','ready','delivered','canceled')),
    queued_at      TEXT NOT NULL,
    started_at     TEXT,
    ready_at       TEXT,
    delivered_at   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    origin_device_id TEXT NOT NULL,
    client_uuid    TEXT NOT NULL UNIQUE,
    is_synced      INTEGER NOT NULL DEFAULT 0,
    synced_at      TEXT,
    FOREIGN KEY (order_id) REFERENCES orders (id)
);
CREATE INDEX IF NOT EXISTS idx_kds_tickets_open
    ON kds_tickets (status, queued_at);
