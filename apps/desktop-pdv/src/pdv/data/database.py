"""Conexão SQLite local e controle transacional.

Escolhas que não são negociáveis num PDV:

* **WAL** — leitura concorrente durante escrita. Sem ele, o worker de
  sincronização travaria a tela do caixa a cada lote enviado.
* **synchronous=FULL** — sim, é mais lento. Também é o que garante que a venda
  já confirmada não some numa queda de energia. Estabelecimento de food service
  perde energia; é o custo de estar certo.
* **foreign_keys=ON** — o SQLite desliga FK por padrão. Sem isso, um item
  órfão de venda entra calado no banco e só aparece no relatório do contador.
* **busy_timeout** — a UI e o sync disputam o arquivo; esperar é melhor que
  estourar `database is locked` na frente do cliente.

Não usamos ORM aqui de propósito: o schema offline é pequeno e o controle
transacional precisa ser explícito (a venda inteira é uma transação só).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final[int] = 13
_SCHEMA_FILE: Final[Path] = Path(__file__).with_name("schema.sql")

#: Migration 2 — tabelas do servidor local (Fase 3).
#:
#: O DDL vive nos dois lugares de propósito: em `schema.sql` para a instalação
#: nova e aqui para a base que já está numa loja. Como tudo é
#: `CREATE ... IF NOT EXISTS`, os dois caminhos convergem para o mesmo estado —
#: e nenhuma loja precisa reinstalar para receber o app do garçom.
_MIGRATION_2_EDGE: Final[str] = """
CREATE TABLE IF NOT EXISTS edge_devices (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    store_id      TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'waiter'
                     CHECK (kind IN ('waiter','kds')),
    token_hash    TEXT NOT NULL,
    operator_id   TEXT,
    paired_at     TEXT NOT NULL,
    last_seen_at  TEXT,
    revoked_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    client_uuid   TEXT NOT NULL UNIQUE,
    is_synced     INTEGER NOT NULL DEFAULT 0,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_edge_devices_token ON edge_devices (token_hash);

CREATE TABLE IF NOT EXISTS edge_pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    used_by     TEXT
);

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
"""

#: Migration 3 — inbox de comandos remotos (Fase 3.5).
#:
#: Mesmo raciocínio da migration 2: o DDL vive aqui e em `schema.sql`, e os
#: dois caminhos convergem porque tudo é `CREATE ... IF NOT EXISTS`. Uma loja
#: instalada não precisa reinstalar para passar a aceitar comando do painel.
_MIGRATION_3_INBOX: Final[str] = """
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
"""


#: Migration 4 — as mesas do salão viram entidade própria.
#:
#: Dividida em três pedaços porque só o primeiro é idempotente. O `ALTER`
#: e o backfill rodam **só** no caminho de upgrade: instalação nova nasce com
#: as colunas pelo `schema.sql` e sem histórico de mesa a resgatar.
_MIGRATION_4_TABLES: Final[str] = """
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
"""

_MIGRATION_4_ORDER_COLUMNS: Final[str] = """

-- `ALTER TABLE ADD COLUMN` não tem `IF NOT EXISTS` no SQLite, então este bloco
-- é o único do arquivo que **não** é idempotente. Só roda no caminho de
-- upgrade; numa base nova as colunas já nascem com o `schema.sql`.
ALTER TABLE orders ADD COLUMN table_id TEXT;

-- O garçom pede a conta; quem recebe é o caixa. Separar o pedido do
-- recebimento é o que mantém o dinheiro num lugar só — deixar o celular
-- "fechar" a mesa criaria um segundo ponto de fechamento sem gaveta, sem
-- impressora e sem conferência de troco.
ALTER TABLE orders ADD COLUMN bill_requested_at TEXT;
"""

_SALON_BACKFILL: Final[str] = """

-- Mesas que só existiam como texto viram linhas de verdade, para que o salão
-- não apareça vazio no dia do update.
INSERT INTO store_tables
    (id, tenant_id, store_id, label, area, seats, sort_order, is_active,
     created_at, updated_at, client_uuid)
SELECT
    lower(hex(randomblob(16))),
    o.tenant_id,
    o.store_id,
    trim(o.customer_id),
    'Salão',
    4,
    0,
    1,
    min(o.created_at),
    min(o.created_at),
    lower(hex(randomblob(16)))
  FROM orders o
 WHERE o.channel = 'waiter'
   AND o.customer_id IS NOT NULL
   AND trim(o.customer_id) <> ''
 GROUP BY o.tenant_id, o.store_id, lower(trim(o.customer_id));

UPDATE orders
   SET table_id = (
        SELECT t.id FROM store_tables t
         WHERE t.tenant_id = orders.tenant_id
           AND t.store_id = orders.store_id
           AND lower(t.label) = lower(trim(orders.customer_id))
   )
 WHERE channel = 'waiter'
   AND customer_id IS NOT NULL
   AND trim(customer_id) <> '';
"""


#: Migration 5 — o freio de autenticação passa a viver em disco.
_MIGRATION_5_THROTTLE: Final[str] = """
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
"""

#: Migration 6 — colunas novas, adicionadas uma a uma e só se faltarem.
#:
#: A migration 4 fez isto como um script de `ALTER TABLE` e pagou o preço: o
#: `ALTER TABLE ADD COLUMN` do SQLite não tem `IF NOT EXISTS`, então rodar duas
#: vezes — ou rodar sobre uma base que já recebeu a coluna por outro caminho —
#: aborta com `duplicate column name` no meio da inicialização do PDV. Conferir
#: o `table_info` antes custa uma consulta por coluna, uma vez na vida da base,
#: e devolve a idempotência que o resto do arquivo tem.
_MIGRATION_6_COLUMNS: Final[tuple[tuple[str, str, str], ...]] = (
    # A gorjeta é do atendimento, não do produto: entra na comanda quando o
    # caixa recebe, e fica FORA do total de venda. Somá-la ao `total_cents`
    # inflaria o faturamento com dinheiro que é da equipe — e o imposto seria
    # calculado sobre ele.
    ("orders", "tip_cents", "INTEGER NOT NULL DEFAULT 0"),
    # Quem lançou o item. A comanda já diz quem a abriu (`operator_id`), mas
    # mesa grande costuma ser atendida por mais de uma pessoa, e sem isto o
    # segundo garçom some da trilha.
    ("order_items", "created_by_user_id", "TEXT"),
)

#: Migration 6 — sessão de garçom no app (Fase 3.8).
_MIGRATION_6_STAFF: Final[str] = """
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

-- A consulta quente é "existe sessão viva neste aparelho?", feita a cada
-- requisição do app.
CREATE INDEX IF NOT EXISTS idx_edge_staff_sessions_device
    ON edge_staff_sessions (device_id, revoked_at, expires_at);
"""

_MIGRATION_7_CASHBACK: Final[str] = """
-- Fase 4 — cashback por lançamentos imutáveis, nunca por saldo editável.
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
"""

_MIGRATION_8_PREPAID: Final[str] = """
-- Fase 4 — crédito pré-pago. Todo saldo é soma de lançamentos imutáveis.
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
"""

_MIGRATION_9_CREDIT_ACCOUNT: Final[str] = """
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
"""

_MIGRATION_10_DISCOUNT_TIERS: Final[str] = """
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
"""

_MIGRATION_11_PROTECTED_DISCOUNT_TIERS: Final[str] = """
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
"""

_MIGRATION_12_OWNER_TIER_ASSIGNMENT: Final[str] = """
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
"""

_MIGRATION_13_FISCAL_FOUNDATION: Final[str] = """
CREATE TABLE IF NOT EXISTS fiscal_series (
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
CREATE TABLE IF NOT EXISTS fiscal_documents (
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
CREATE INDEX IF NOT EXISTS idx_fiscal_documents_pending
    ON fiscal_documents(status,is_synced,issued_at);
CREATE TABLE IF NOT EXISTS fiscal_events (
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
CREATE TRIGGER IF NOT EXISTS trg_fiscal_documents_no_delete
BEFORE DELETE ON fiscal_documents
BEGIN
    SELECT RAISE(ABORT, 'fiscal documents are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_fiscal_events_no_update
BEFORE UPDATE ON fiscal_events
BEGIN
    SELECT RAISE(ABORT, 'fiscal events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_fiscal_events_no_delete
BEFORE DELETE ON fiscal_events
BEGIN
    SELECT RAISE(ABORT, 'fiscal events are immutable');
END;
"""


def _add_column(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    """Acrescenta a coluna se ela ainda não existir.

    Os nomes vêm de `_MIGRATION_6_COLUMNS`, constante deste módulo, e nunca de
    entrada — não há interpolação de dado externo aqui. O SQLite não aceita
    parâmetro em DDL, então a interpolação é a única forma.
    """
    existing = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in existing:
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


class Database:
    """Dona da conexão. Uma instância por processo."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # Uma conexão POR THREAD.
        #
        # `BEGIN IMMEDIATE` é estado da conexão, não do processo: duas threads
        # transacionando na mesma conexão colidem em
        # "cannot start a transaction within a transaction" e uma das duas
        # perde a gravação. E o PDV tem três escritores concorrentes — a UI do
        # caixa, a thread de sincronização e o servidor local do salão —, então
        # isto não é hipótese: é o garçom lançando um pedido enquanto a
        # operadora registra uma pesagem.
        #
        # O WAL é o que torna isso barato: leitores não bloqueiam o escritor, e
        # `busy_timeout` faz o segundo escritor esperar em vez de falhar.
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    # -- ciclo de vida -------------------------------------------------------- #

    def connect(self) -> sqlite3.Connection:
        existing: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if existing is not None:
            return existing

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            timeout=30.0,
            isolation_level=None,  # controlamos BEGIN/COMMIT explicitamente
            # A conexão pertence a uma thread só; a exceção continua liberada
            # porque objetos de repositório recebem a conexão por parâmetro e
            # não há como o SQLite saber que quem a usa é a mesma thread.
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row

        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA temp_store = MEMORY")

        self._local.connection = connection
        with self._lock:
            self._all.append(connection)
        return connection

    def close(self) -> None:
        """Fecha as conexões de todas as threads."""
        with self._lock:
            connections, self._all = self._all, []

        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - encerrando de qualquer jeito
                pass
        self._local = threading.local()

    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    # -- migrations ----------------------------------------------------------- #

    def migrate(self) -> None:
        """Aplica o schema de forma idempotente, versionado por `user_version`.

        Migrations futuras entram como blocos numerados aqui — nunca editando
        `schema.sql` em cima de uma base já instalada em loja.
        """
        connection = self.connect()
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])

        if current < 1:
            connection.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))

        if current < 2:
            connection.executescript(_MIGRATION_2_EDGE)

        if current < 3:
            connection.executescript(_MIGRATION_3_INBOX)

        if current < 4:
            if current > 0:
                connection.executescript(_MIGRATION_4_ORDER_COLUMNS)
            connection.executescript(_MIGRATION_4_TABLES)
            if current > 0:
                connection.executescript(_SALON_BACKFILL)

        if current < 5:
            connection.executescript(_MIGRATION_5_THROTTLE)

        if current < 6:
            for table, column, declaration in _MIGRATION_6_COLUMNS:
                _add_column(connection, table, column, declaration)
            connection.executescript(_MIGRATION_6_STAFF)

        if current < 7:
            connection.executescript(_MIGRATION_7_CASHBACK)

        if current < 8:
            connection.executescript(_MIGRATION_8_PREPAID)

        if current < 9:
            connection.executescript(_MIGRATION_9_CREDIT_ACCOUNT)

        if current < 10:
            connection.executescript(_MIGRATION_10_DISCOUNT_TIERS)

        if current < 11:
            connection.executescript(_MIGRATION_11_PROTECTED_DISCOUNT_TIERS)

        if current < 12:
            connection.executescript(_MIGRATION_12_OWNER_TIER_ASSIGNMENT)

        if current < 13:
            connection.executescript(_MIGRATION_13_FISCAL_FOUNDATION)

        if current < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- transação ------------------------------------------------------------ #

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transação atômica — tudo ou nada.

        É aqui que mora a garantia do offline: item de venda, movimentos de
        estoque, ingredientes consumidos, ledger de auditoria e as linhas do
        outbox entram **juntos** ou não entram. Não existe estado intermediário
        onde o estoque baixou mas a venda não existe.

        ``IMMEDIATE`` adquire o lock de escrita no BEGIN em vez de na primeira
        escrita, evitando `SQLITE_BUSY` no meio de uma transação já iniciada.
        """
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    # -- utilidades ----------------------------------------------------------- #

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def next_counter(self, connection: sqlite3.Connection, name: str) -> int:
        """Incrementa e devolve uma sequência local, dentro da transação do chamador.

        Usada pelo número da venda (único por terminal) e pelo `seq` do ledger.
        Precisa rodar na mesma transação de quem consome o número, senão dois
        caixas concorrentes recebem o mesmo valor.
        """
        connection.execute(
            "INSERT INTO local_counters (name, value) VALUES (?, 1) "
            "ON CONFLICT (name) DO UPDATE SET value = value + 1",
            (name,),
        )
        row = connection.execute(
            "SELECT value FROM local_counters WHERE name = ?", (name,)
        ).fetchone()
        return int(row["value"])
