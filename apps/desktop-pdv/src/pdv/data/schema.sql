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
    -- Quem lançou. A comanda diz quem a abriu; mesa grande costuma ser
    -- atendida por mais de uma pessoa, e sem isto o segundo garçom some.
    created_by_user_id    TEXT,
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

-- Fase 4 — cliente e cashback. O saldo é derivado do ledger; não existe
-- coluna de saldo que possa ser sobrescrita sem deixar história.
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
    phone TEXT, is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers (tenant_id, phone) WHERE phone IS NOT NULL;

CREATE TABLE IF NOT EXISTS cashback_rules (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    percent_basis_points INTEGER NOT NULL CHECK(percent_basis_points BETWEEN 0 AND 10000),
    max_per_sale_cents INTEGER NOT NULL DEFAULT 0,
    validity_days INTEGER NOT NULL CHECK(validity_days BETWEEN 1 AND 3650),
    is_active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, store_id)
);

CREATE TABLE IF NOT EXISTS cashback_ledger (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_cashback_credit_order
    ON cashback_ledger (tenant_id, order_id) WHERE entry_type = 'credit';
CREATE INDEX IF NOT EXISTS idx_cashback_customer
    ON cashback_ledger (tenant_id, customer_id, created_at);

CREATE TABLE IF NOT EXISTS prepaid_ledger (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    customer_id TEXT NOT NULL, entry_type TEXT NOT NULL
        CHECK(entry_type IN ('deposit','debit','refund')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), order_id TEXT,
    actor_user_id TEXT NOT NULL, authorizer_user_id TEXT,
    created_at TEXT NOT NULL, client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0, synced_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prepaid_debit_order
    ON prepaid_ledger(tenant_id, order_id) WHERE entry_type='debit';
CREATE INDEX IF NOT EXISTS idx_prepaid_customer
    ON prepaid_ledger(tenant_id, customer_id, created_at);

CREATE TABLE IF NOT EXISTS customer_credit_accounts (
    customer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
    limit_cents INTEGER NOT NULL CHECK(limit_cents >= 0),
    due_days INTEGER NOT NULL CHECK(due_days BETWEEN 1 AND 365),
    is_active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS credit_account_ledger (
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_charge_order
    ON credit_account_ledger(tenant_id,order_id) WHERE entry_type='charge';
CREATE INDEX IF NOT EXISTS idx_credit_customer_due
    ON credit_account_ledger(tenant_id,customer_id,due_at,created_at);

CREATE TABLE IF NOT EXISTS discount_tiers (
    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, store_id TEXT NOT NULL,
    code TEXT NOT NULL, name TEXT NOT NULL,
    percent_basis_points INTEGER NOT NULL CHECK(percent_basis_points BETWEEN 0 AND 10000),
    priority INTEGER NOT NULL DEFAULT 0, requires_manager INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT, valid_until TEXT, is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL, client_uuid TEXT NOT NULL UNIQUE,
    is_synced INTEGER NOT NULL DEFAULT 0, synced_at TEXT,
    UNIQUE(tenant_id,store_id,code)
);
CREATE TABLE IF NOT EXISTS customer_discount_tiers (
    customer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tier_id TEXT NOT NULL,
    assigned_by_user_id TEXT NOT NULL, assigned_at TEXT NOT NULL,
    client_uuid TEXT NOT NULL UNIQUE, is_synced INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT, FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(tier_id) REFERENCES discount_tiers(id)
);

-- Funcionário e Dono representam vínculos permanentes, não progressão de
-- fidelidade. A regra no banco também barra SQL direto e código antigo.
CREATE TRIGGER IF NOT EXISTS trg_protected_discount_tier_no_change
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
CREATE TRIGGER IF NOT EXISTS trg_protected_discount_tier_no_delete
BEFORE DELETE ON customer_discount_tiers
WHEN EXISTS (
    SELECT 1 FROM discount_tiers
    WHERE id = OLD.tier_id AND tenant_id = OLD.tenant_id
      AND code IN ('employee', 'owner')
 )
BEGIN
    SELECT RAISE(ABORT, 'protected discount tier cannot be removed');
END;

-- Mesmo um INSERT direto ou uma linha recebida de sincronização precisa
-- nomear um proprietário ativo como responsável pela classificação especial.
CREATE TRIGGER IF NOT EXISTS trg_protected_tier_requires_owner_insert
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
CREATE TRIGGER IF NOT EXISTS trg_protected_tier_requires_owner_update
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

-- ===========================================================================
-- Fase 3.5 — Inbox de comandos remotos (painel administrativo)
-- ===========================================================================

-- Espelho do `sync_outbox`, na direção contrária. Aqui o terminal **recebe**
-- ordens de fora, o que inverte o modelo de confiança de todo o resto do
-- sistema: até a Fase 3 o PDV só enviava.
--
-- `command_uuid` como PRIMARY KEY é a âncora de idempotência, igual ao
-- `client_uuid` no outbox: reentregar o mesmo comando — porque a resposta do
-- terminal se perdeu, ou porque a nuvem reenviou por timeout — colide na
-- chave e não concede o desconto duas vezes.
--
-- `settled_at` e `reported_at` são separados de propósito. O terminal aplica
-- primeiro e avisa a nuvem depois; se o aviso se perder, ele reavisa — mas
-- nunca reaplica, porque o status já saiu de `pending`.
CREATE TABLE IF NOT EXISTS remote_commands (
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
    reported_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_remote_commands_pending
    ON remote_commands (status, received_at);

-- Resultados ainda não confirmados pela nuvem.
CREATE INDEX IF NOT EXISTS idx_remote_commands_unreported
    ON remote_commands (reported_at, settled_at);

-- ===========================================================================
-- Fase 3.6 — Mesas do salão
-- ===========================================================================

-- Até aqui "mesa" era texto livre enfiado em `orders.customer_id`. Funcionava
-- para provar o fluxo, e quebra assim que o salão é real:
--
--   * dois garçons abriam a "Mesa 5" duas vezes, e a conta saía partida em
--     duas comandas que ninguém consegue juntar na hora de cobrar;
--   * "mesa 5", "Mesa 5" e "M5" viravam três mesas diferentes;
--   * não havia o que configurar — nem quantas mesas a loja tem, nem onde.
--
-- A mesa vira entidade própria. O rótulo continua copiado no pedido como
-- **histórico**: renomear a "Mesa 5" para "Varanda 2" amanhã não pode
-- reescrever o que saiu impresso no cupom de ontem.
CREATE TABLE IF NOT EXISTS store_tables (
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

-- Duas mesas ativas com o mesmo nome é o erro de digitação que vira conta
-- trocada. `lower(label)` porque o garçom digita como quiser.
CREATE UNIQUE INDEX IF NOT EXISTS idx_store_tables_label
    ON store_tables (tenant_id, store_id, lower(label))
    WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_store_tables_map
    ON store_tables (tenant_id, store_id, is_active, sort_order);

CREATE INDEX IF NOT EXISTS idx_orders_table
    ON orders (tenant_id, table_id, status);

-- ===========================================================================
-- Fase 3.7 — Freio de autenticação persistente
-- ===========================================================================

-- O bloqueio por tentativas vivia **só em memória**, num dicionário do
-- `AuthorizationService`. Isso o tornava decorativo: matar o processo e abrir
-- de novo zerava o contador, e cada reabertura dava mais cinco tentativas
-- livres. Com Argon2id a 37 ms por tentativa, as 10 000 combinações de um PIN
-- de 4 dígitos caem em ~6 minutos por esse caminho.
--
-- `scope` guarda `login:<nome>` para o freio por usuário e `*` para o global.
-- O global existe porque o freio por usuário sozinho não impede espalhar as
-- tentativas por vários logins, cada um com sua cota livre.
--
-- Os instantes são de relógio de parede porque precisam sobreviver ao
-- processo. Quem tem administrador da máquina consegue atrasar o relógio e
-- encurtar o bloqueio — e também consegue editar este banco direto, então a
-- defesa contra esse perfil nunca foi local (ver o cabeçalho de
-- `services/authorization.py`). Dentro de uma sessão o serviço ainda mantém um
-- piso monôtonico, que o relógio não move.
CREATE TABLE IF NOT EXISTS auth_throttle (
    scope            TEXT PRIMARY KEY,
    failures         INTEGER NOT NULL DEFAULT 0,
    locked_until     TEXT,
    first_failure_at TEXT NOT NULL,
    last_failure_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_throttle_locked
    ON auth_throttle (locked_until);

-- ===========================================================================
-- Fase 3.8 — O garçom entra com a credencial dele
-- ===========================================================================

-- Até aqui o app tinha **uma** identidade: o aparelho pareado. O pedido era
-- atribuído ao celular, e por isso não havia como fechar resultado nem gorjeta
-- por pessoa — três garçons revezando o mesmo tablet produziam uma coluna só.
--
-- Esta tabela guarda a sessão da **pessoa**, em cima do pareamento do
-- aparelho. As duas continuam existindo e respondem a perguntas diferentes:
-- o token do aparelho diz *de onde* veio o lançamento, o da sessão diz *quem*
-- lançou. Um celular roubado sem o PIN de ninguém não lança nada; um PIN
-- vazado sem aparelho pareado também não.
--
-- Por que em disco, ao contrário da concessão de gerente
-- ------------------------------------------------------
-- `edge/manager.py` guarda a concessão só em memória, e de propósito: ela é
-- **poder** (cancelar comanda), e poder que sobrevive a um restart sobrevive
-- também a um restart provocado. Esta sessão é **identidade**, e não concede
-- nada que o aparelho pareado já não pudesse fazer — só nomeia quem age. Se
-- ela evaporasse a cada reinício do PDV, a loja inteira teria de redigitar PIN
-- no meio do serviço, e o caminho de menor resistência viraria deixar um
-- login só aberto para todos: exatamente o problema que isto veio resolver.
CREATE TABLE IF NOT EXISTS edge_staff_sessions (
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

CREATE INDEX IF NOT EXISTS idx_edge_staff_sessions_device
    ON edge_staff_sessions (device_id, revoked_at, expires_at);
