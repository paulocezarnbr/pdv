-- Gerado por apps/desktop-pdv/tests/test_schema_contract.py. Não edite à mão.
-- Schema local do PDV na versão 14, como o migrate() o deixa.
CREATE TABLE products (
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

CREATE INDEX idx_products_tenant ON products (tenant_id, is_active);

CREATE INDEX idx_products_barcode ON products (barcode);

CREATE TABLE recipes (
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

CREATE TABLE recipe_lines (
    id                 TEXT PRIMARY KEY,
    recipe_id          TEXT NOT NULL,
    inventory_item_id  TEXT NOT NULL,
    qty_per_base_mg    INTEGER NOT NULL CHECK (qty_per_base_mg >= 0),
    waste_percent      TEXT NOT NULL DEFAULT '0',
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (recipe_id) REFERENCES recipes (id),
    FOREIGN KEY (inventory_item_id) REFERENCES inventory_items (id)
);

CREATE INDEX idx_recipe_lines_recipe ON recipe_lines (recipe_id);

CREATE TABLE inventory_items (
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

CREATE TABLE stock_movements (
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

CREATE INDEX idx_stock_mov_pending ON stock_movements (is_synced, created_at);

CREATE INDEX idx_stock_mov_item ON stock_movements (inventory_item_id);

CREATE TABLE orders (
    id                     TEXT PRIMARY KEY,
    tenant_id              TEXT NOT NULL,
    store_id               TEXT NOT NULL,
    device_id              TEXT NOT NULL,
    local_number           INTEGER NOT NULL,      -- sequência POR DEVICE
    channel                TEXT NOT NULL DEFAULT 'counter',
    status                 TEXT NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','paid','canceled')),
    customer_id            TEXT,
    table_id               TEXT,          -- mesa do salão (ver store_tables)
    bill_requested_at      TEXT,          -- o garçom pediu a conta; quem recebe é o caixa
    operator_id            TEXT NOT NULL,
    subtotal_cents         INTEGER NOT NULL DEFAULT 0,
    discount_cents         INTEGER NOT NULL DEFAULT 0,
    total_cents            INTEGER NOT NULL DEFAULT 0,
    -- A gorjeta é do atendimento, não do produto, e fica FORA do total: somá-la
    -- ao faturamento cobraria imposto sobre dinheiro que é da equipe.
    tip_cents              INTEGER NOT NULL DEFAULT 0,
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

CREATE UNIQUE INDEX idx_orders_local_number
    ON orders (device_id, local_number);

CREATE INDEX idx_orders_pending ON orders (is_synced, created_at);

CREATE TABLE fiscal_series (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    model INTEGER NOT NULL DEFAULT 65 CHECK(model IN (59,65)),
    series INTEGER NOT NULL CHECK(series BETWEEN 1 AND 999),
    next_number INTEGER NOT NULL DEFAULT 1 CHECK(next_number > 0),
    environment TEXT NOT NULL DEFAULT 'homologation'
        CHECK(environment IN ('homologation','production')),
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id,store_id,device_id,model),
    UNIQUE(tenant_id,store_id,model,series)
);

CREATE TABLE fiscal_documents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    model INTEGER NOT NULL CHECK(model IN (59,65)),
    series INTEGER NOT NULL CHECK(series BETWEEN 1 AND 999),
    number INTEGER NOT NULL CHECK(number > 0),
    environment TEXT NOT NULL CHECK(environment IN ('homologation','production')),
    emission_type TEXT NOT NULL CHECK(emission_type IN ('normal','offline_contingency')),
    status TEXT NOT NULL CHECK(status IN
        ('pending','contingency_pending','authorized','rejected','canceled')),
    contingency_reason TEXT,
    access_key TEXT,
    protocol TEXT,
    xml_content TEXT,
    issued_at TEXT NOT NULL,
    authorized_at TEXT,
    updated_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id),
    UNIQUE(tenant_id,store_id,model,series,number),
    UNIQUE(tenant_id,order_id,model)
);

CREATE INDEX idx_fiscal_documents_pending
    ON fiscal_documents(status,is_synced,issued_at);

CREATE TABLE fiscal_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    fiscal_document_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    FOREIGN KEY(fiscal_document_id) REFERENCES fiscal_documents(id)
);

CREATE TRIGGER trg_fiscal_documents_no_delete
BEFORE DELETE ON fiscal_documents BEGIN
    SELECT RAISE(ABORT, 'fiscal documents are immutable');
END;

CREATE TRIGGER trg_fiscal_events_no_update
BEFORE UPDATE ON fiscal_events BEGIN
    SELECT RAISE(ABORT, 'fiscal events are immutable');
END;

CREATE TRIGGER trg_fiscal_events_no_delete
BEFORE DELETE ON fiscal_events BEGIN
    SELECT RAISE(ABORT, 'fiscal events are immutable');
END;

CREATE TABLE order_items (
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
    -- Quem lançou. A comanda diz quem a abriu; mesa grande costuma ser
    -- atendida por mais de uma pessoa, e sem isto o segundo garçom some.
    created_by_user_id    TEXT,
    created_at            TEXT NOT NULL,
    client_uuid           TEXT NOT NULL UNIQUE,
    is_synced             INTEGER NOT NULL DEFAULT 0,
    synced_at             TEXT,
    FOREIGN KEY (order_id) REFERENCES orders (id)
);

CREATE INDEX idx_order_items_order ON order_items (order_id);

CREATE TABLE order_item_ingredients (
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

CREATE TABLE payments (
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

CREATE TABLE audit_ledger (
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

CREATE UNIQUE INDEX idx_audit_device_seq ON audit_ledger (device_id, seq);

CREATE INDEX idx_audit_pending ON audit_ledger (is_synced, seq);

CREATE TRIGGER trg_audit_no_update
BEFORE UPDATE ON audit_ledger
FOR EACH ROW WHEN OLD.hash <> NEW.hash OR OLD.payload_json <> NEW.payload_json
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger e imutavel');
END;

CREATE TRIGGER trg_audit_no_delete
BEFORE DELETE ON audit_ledger
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger e imutavel');
END;

CREATE TABLE cash_sessions (
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

CREATE TABLE sync_outbox (
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

CREATE INDEX idx_outbox_ready ON sync_outbox (available_at, seq);

CREATE TABLE sync_cursors (
    entity_table     TEXT PRIMARY KEY,
    last_server_seq  INTEGER NOT NULL DEFAULT 0,
    last_pulled_at   TEXT
);

CREATE TABLE device_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE local_counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE users (
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

CREATE TABLE customers (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
    phone TEXT, is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT
);

CREATE UNIQUE INDEX idx_customers_phone
    ON customers (tenant_id, phone) WHERE phone IS NOT NULL;

CREATE TABLE cashback_rules (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    percent_basis_points INTEGER NOT NULL CHECK(percent_basis_points BETWEEN 0 AND 10000),
    max_per_sale_cents INTEGER NOT NULL DEFAULT 0,
    validity_days INTEGER NOT NULL CHECK(validity_days BETWEEN 1 AND 3650),
    is_active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, store_id)
);

CREATE TABLE cashback_ledger (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    customer_id TEXT NOT NULL, order_id TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('credit','debit')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    source_credit_id TEXT, expires_at TEXT, created_at TEXT NOT NULL,
    actor_user_id TEXT NOT NULL, client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0, synced_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(source_credit_id) REFERENCES cashback_ledger(id)
);

CREATE UNIQUE INDEX idx_cashback_credit_order
    ON cashback_ledger (tenant_id, order_id) WHERE entry_type = 'credit';

CREATE INDEX idx_cashback_customer
    ON cashback_ledger (tenant_id, customer_id, created_at);

CREATE TABLE prepaid_ledger (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    customer_id TEXT NOT NULL, entry_type TEXT NOT NULL
        CHECK(entry_type IN ('deposit','debit','refund')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), order_id TEXT,
    actor_user_id TEXT NOT NULL, authorizer_user_id TEXT,
    created_at TEXT NOT NULL, client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0, synced_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE UNIQUE INDEX idx_prepaid_debit_order
    ON prepaid_ledger(tenant_id, order_id) WHERE entry_type='debit';

CREATE INDEX idx_prepaid_customer
    ON prepaid_ledger(tenant_id, customer_id, created_at);

CREATE TABLE customer_credit_accounts (
    customer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
    limit_cents INTEGER NOT NULL CHECK(limit_cents >= 0),
    due_days INTEGER NOT NULL CHECK(due_days BETWEEN 1 AND 365),
    is_active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE credit_account_ledger (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    customer_id TEXT NOT NULL, entry_type TEXT NOT NULL
        CHECK(entry_type IN ('charge','payment','forgive')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), order_id TEXT,
    source_charge_id TEXT, due_at TEXT, actor_user_id TEXT NOT NULL,
    authorizer_user_id TEXT, created_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT, FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(source_charge_id) REFERENCES credit_account_ledger(id)
);

CREATE UNIQUE INDEX idx_credit_charge_order
    ON credit_account_ledger(tenant_id,order_id) WHERE entry_type='charge';

CREATE INDEX idx_credit_customer_due
    ON credit_account_ledger(tenant_id,customer_id,due_at,created_at);

CREATE TABLE discount_tiers (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT NOT NULL,
    percent_basis_points INTEGER NOT NULL CHECK(percent_basis_points BETWEEN 0 AND 10000),
    priority INTEGER NOT NULL DEFAULT 0, requires_manager INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT, valid_until TEXT, is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL, client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0, synced_at TEXT,
    UNIQUE(tenant_id,store_id,code)
);

CREATE TABLE customer_discount_tiers (
    customer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tier_id TEXT NOT NULL,
    assigned_by_user_id TEXT NOT NULL, assigned_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT, FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(tier_id) REFERENCES discount_tiers(id)
);

CREATE TRIGGER trg_protected_discount_tier_no_change
BEFORE UPDATE OF tier_id ON customer_discount_tiers
WHEN OLD.tier_id <> NEW.tier_id
 AND EXISTS (
    SELECT 1 FROM discount_tiers
    WHERE id = OLD.tier_id AND tenant_id = OLD.tenant_id
      AND code IN ('employee', 'owner')
 )
BEGIN
    SELECT RAISE(ABORT, 'protected discount tier cannot be changed');
END;

CREATE TRIGGER trg_protected_discount_tier_no_delete
BEFORE DELETE ON customer_discount_tiers
WHEN EXISTS (
    SELECT 1 FROM discount_tiers
    WHERE id = OLD.tier_id AND tenant_id = OLD.tenant_id
      AND code IN ('employee', 'owner')
 )
BEGIN
    SELECT RAISE(ABORT, 'protected discount tier cannot be removed');
END;

CREATE TRIGGER trg_protected_tier_requires_owner_insert
BEFORE INSERT ON customer_discount_tiers
WHEN EXISTS (
    SELECT 1 FROM discount_tiers
    WHERE id=NEW.tier_id AND tenant_id=NEW.tenant_id
      AND code IN ('employee','owner')
 ) AND NOT EXISTS (
    SELECT 1 FROM users
    WHERE id=NEW.assigned_by_user_id AND tenant_id=NEW.tenant_id
      AND role='owner' AND is_active=1
 )
BEGIN
    SELECT RAISE(ABORT, 'protected discount tier requires owner');
END;

CREATE TRIGGER trg_protected_tier_requires_owner_update
BEFORE UPDATE OF tier_id, assigned_by_user_id ON customer_discount_tiers
WHEN EXISTS (
    SELECT 1 FROM discount_tiers
    WHERE id=NEW.tier_id AND tenant_id=NEW.tenant_id
      AND code IN ('employee','owner')
 ) AND NOT EXISTS (
    SELECT 1 FROM users
    WHERE id=NEW.assigned_by_user_id AND tenant_id=NEW.tenant_id
      AND role='owner' AND is_active=1
 )
BEGIN
    SELECT RAISE(ABORT, 'protected discount tier requires owner');
END;

CREATE TABLE edge_devices (
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

CREATE INDEX idx_edge_devices_token ON edge_devices (token_hash);

CREATE TABLE edge_pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    used_by     TEXT
);

CREATE TABLE kds_tickets (
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

CREATE INDEX idx_kds_tickets_open
    ON kds_tickets (status, queued_at);

CREATE TABLE remote_commands (
    command_uuid      TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    store_id          TEXT NOT NULL,
    device_id         TEXT NOT NULL,          -- terminal alvo
    kind              TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    issued_by_user_id TEXT NOT NULL,
    issued_by_name    TEXT NOT NULL,
    issued_at         TEXT NOT NULL,
    signature         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','applied','refused')),
    received_at       TEXT NOT NULL,
    settled_at        TEXT,
    result_message    TEXT,
    reported_at       TEXT,
    -- Migration 14: comando de risco esperando aceite de quem está no caixa.
    confirmation_requested_at TEXT,
    confirmation_note         TEXT,
    confirmation_reported_at  TEXT
);

CREATE INDEX idx_remote_commands_pending
    ON remote_commands (status, received_at);

CREATE INDEX idx_remote_commands_unreported
    ON remote_commands (reported_at, settled_at);

CREATE TABLE store_tables (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    label       TEXT NOT NULL,
    area        TEXT NOT NULL DEFAULT 'Salão',
    seats       INTEGER NOT NULL DEFAULT 4,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    -- Mesa retirada do mapa **nunca** é apagada: comandas antigas apontam para
    -- ela, e o relatório de faturamento por mesa perderia o passado.
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE,
    is_synced   INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT
);

CREATE UNIQUE INDEX idx_store_tables_label
    ON store_tables (tenant_id, store_id, lower(label))
    WHERE is_active = 1;

CREATE INDEX idx_store_tables_map
    ON store_tables (tenant_id, store_id, is_active, sort_order);

CREATE INDEX idx_orders_table
    ON orders (tenant_id, table_id, status);

CREATE TABLE auth_throttle (
    scope            TEXT PRIMARY KEY,
    failures         INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT,
    first_failure_at TEXT NOT NULL,
    last_failure_at  TEXT NOT NULL
);

CREATE INDEX idx_auth_throttle_locked
    ON auth_throttle (locked_until);

CREATE TABLE edge_staff_sessions (
    token_hash    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    device_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    user_name     TEXT NOT NULL,
    user_login    TEXT NOT NULL,
    role          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,
    FOREIGN KEY (device_id) REFERENCES edge_devices (id)
);

CREATE INDEX idx_edge_staff_sessions_device
    ON edge_staff_sessions (device_id, revoked_at, expires_at);

PRAGMA user_version = 14;
