# der.md — Modelo de Dados (DER)

> Convenções aplicadas a **todas** as tabelas de negócio. Se uma tabela não
> seguir, é bug de modelagem.

## 0. Colunas Canônicas

| Coluna | Tipo (Cloud) | Tipo (SQLite) | Função |
|---|---|---|---|
| `id` | `UUID` PK | `TEXT` PK | UUIDv7 gerado no **cliente**. Monotônico no tempo → índice saudável |
| `tenant_id` | `UUID NOT NULL` | `TEXT NOT NULL` | Isolamento multi-tenant. Alvo da RLS |
| `store_id` | `UUID NOT NULL` | `TEXT NOT NULL` | Loja/filial dentro do tenant |
| `created_at` | `TIMESTAMPTZ` | `TEXT` (ISO-8601 UTC) | Relógio do **cliente** — não confiável, mas necessário |
| `updated_at` | `TIMESTAMPTZ` | `TEXT` | Base do LWW e do pull incremental |
| `deleted_at` | `TIMESTAMPTZ NULL` | `TEXT NULL` | Soft delete; exclusão também se replica |
| `client_uuid` | `UUID UNIQUE(tenant)` | `TEXT` | **Chave de idempotência**. `UNIQUE (tenant_id, client_uuid)` |
| `is_synced` | — | `INTEGER 0/1` | Só existe local. `1` apenas com ACK do servidor |
| `synced_at` | — | `TEXT NULL` | Momento do ACK. `NULL` = pendente |
| `sync_version` | `BIGINT` | `INTEGER` | Incrementa a cada alteração; detecta update perdido |
| `server_seq` | `BIGSERIAL` | `INTEGER NULL` | Ordem global do servidor; desempate de LWW e cursor de pull |
| `origin_device_id` | `UUID` | `TEXT` | Qual PDV/celular originou a linha (auditoria e replay) |

**Colunas de sincronização por tipo de tabela**

| Tipo | Exemplos | Colunas de sync obrigatórias | Conflito |
|---|---|---|---|
| **Append-only transacional** | `orders`, `order_items`, `payments`, `stock_movements`, `audit_ledger` | `client_uuid`, `is_synced`, `synced_at`, `origin_device_id` | Impossível — só deduplicação |
| **Cadastro mutável** | `products`, `customers`, `recipes`, `inventory_items` | + `updated_at`, `sync_version`, `server_seq` | LWW por `updated_at`, desempate `server_seq` |
| **Local-only** | `sync_outbox`, `device_settings`, `cash_drawer_state` | — | Nunca sobe como entidade |

---

## 1. Diagrama de Relacionamentos

```
tenants ─┬─< stores ─┬─< devices ─────< cash_sessions ──< cash_movements
         │           │                        │
         │           ├─< users ───────────────┤
         │           │                        │
         │           ├─< orders ──┬─< order_items ──< order_item_ingredients
         │           │            ├─< payments
         │           │            └─< order_status_events (KDS)
         │           │
         │           ├─< inventory_items ──< stock_movements
         │           │        ^
         │           │        └──< recipe_lines >── recipes ──< products
         │           │
         │           ├─< tables ──< tabs (comandas)
         │           └─< audit_ledger
         │
         ├─< tenant_modules            (licenciamento modular)
         ├─< customers ─┬─< customer_credit_ledger   (pré-pago)
         │              ├─< cashback_ledger
         │              ├─< credit_accounts ──< credit_entries  (fiado)
         │              └─> discount_tiers           (Diamante/Funcionário/Dono)
         └─< discount_tiers
```

---

## 2. Tabelas

### 2.1 Core / Tenancy

**`tenants`** — o cliente SaaS.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `legal_name`, `trade_name` | TEXT | |
| `document` | TEXT | CNPJ/CPF |
| `plan` | TEXT | `starter` \| `pro` \| `enterprise` |
| `status` | TEXT | `active` \| `suspended` \| `canceled` |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

**`tenant_modules`** — liga/desliga módulo por tenant (modularidade real).

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK | |
| `module_code` | TEXT | `M03`, `M07`, `M12`… |
| `enabled` | BOOL | |
| `config` | JSONB | Parâmetros do módulo |
| `valid_until` | TIMESTAMPTZ NULL | Expiração da licença |

**`stores`** — loja/filial. `id`, `tenant_id`, `name`, `timezone`, `address`, `fiscal_config` (JSONB).

**`devices`** — cada PDV, celular ou KDS.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | Gerado na ativação |
| `tenant_id`, `store_id` | UUID FK | |
| `kind` | TEXT | `pdv_desktop` \| `waiter_mobile` \| `kds` |
| `serial_prefix` | TEXT | Série fiscal exclusiva do PDV |
| `last_sync_at` | TIMESTAMPTZ | Monitoramento de caixa mudo |
| `clock_drift_seconds` | INT | Diferença medida no último sync |
| `token_hash` | TEXT | SHA-256 do token de sync. **Nunca o token em texto** |
| `hostname`, `os`, `arch` | TEXT | Rótulo para o painel distinguir terminais. Forjável: não é controle de segurança |
| `activated_at` | TIMESTAMPTZ | Última ativação bem-sucedida |

> **O `device_secret` não está aqui, e isso é deliberado.** A chave HMAC que
> torna o ledger de auditoria verificável é gerada **no terminal** e protegida
> por DPAPI; não trafega e não é conhecida pelo servidor. O que o servidor
> guarda é a âncora (último `seq` e `hash` aceitos por dispositivo), que é o que
> torna o passado imutável mesmo se a máquina da loja for comprometida.

**`device_activation_codes`** — pareamento de uso único.

| Campo | Tipo | Notas |
|---|---|---|
| `code_hash` | TEXT PK | SHA-256 do código. Um dump do banco não entrega códigos ativos |
| `tenant_id`, `store_id`, `device_id` | UUID | Identidade que o código concede |
| `created_at` | TIMESTAMPTZ | Validade de 15 min é contada daqui |
| `used_at`, `used_by_ip` | TIMESTAMPTZ, INET | Preenchidos no consumo atômico |
| `revoked_at` | TIMESTAMPTZ | Queimar um código ditado por engano custa um clique |

O consumo é `UPDATE ... WHERE used_at IS NULL ... RETURNING`: ler-e-depois-gravar
abriria a janela em que dois terminais recebem a mesma identidade.

**`device_activation_attempts`** — `ip`, `attempted_at`. Teto de 10 tentativas por
IP em 15 minutos. Sem ele, um código de 8 caracteres cai por força bruta dentro
da própria validade.

**`users`** — `id`, `tenant_id`, `name`, `login`, `password_hash` (Argon2id),
`role`, `pin_hash` (autorização rápida no PDV), `discount_tier_id`, `is_active`.
`password_hash` e `pin_hash` são replicados ao PDV para **autorizar offline**.

---

### 2.2 Catálogo e Ficha Técnica

**`products`**

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID | |
| `sku`, `barcode` | TEXT | Código de balança (EAN-13 prefixo 2) aceito |
| `name` | TEXT | |
| `category_id` | UUID FK | |
| `pricing_mode` | TEXT | **`unit`** \| **`weight`** ← chave do produto pesável |
| `price_cents` | INT | Se `unit`: preço do item. Se `weight`: **preço por kg** |
| `tare_grams` | INT | Embalagem descontada do peso bruto |
| `recipe_id` | UUID FK NULL | Ficha técnica |
| `is_active` | BOOL | |
| `updated_at`, `sync_version`, `server_seq` | | LWW |

**`recipes`** — `id`, `tenant_id`, `product_id`, `base_qty_g` (rendimento-base da
ficha, ex.: 1000 g), `yield_factor` (perda de cocção), `updated_at`, `sync_version`.

**`recipe_lines`** — a conversão que permite a **baixa fracionada**.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `recipe_id` | UUID FK | |
| `inventory_item_id` | UUID FK | |
| `qty_per_base_mg` | **BIGINT** | Quantidade do insumo por `base_qty_g`, em **mg/ml** |
| `waste_percent` | NUMERIC(5,2) | Perda esperada |

> Baixa: `consumo_mg = qty_per_base_mg × peso_vendido_g / base_qty_g`,
> com `ROUND_HALF_UP` aplicado **uma única vez**, no fim.

**`inventory_items`** — insumo. `id`, `tenant_id`, `store_id`, `name`,
`unit` (`mg` \| `ml` \| `un`), `min_stock_mg`, `avg_cost_cents_per_kg`, `updated_at`.

**`stock_movements`** — **append-only**, saldo é derivado.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id`, `store_id` | UUID | |
| `inventory_item_id` | UUID FK | |
| `qty_mg` | BIGINT | **Negativo** = saída |
| `movement_type` | TEXT | `sale` \| `purchase` \| `waste` \| `adjustment` \| `inventory` |
| `reference_type`, `reference_id` | TEXT / UUID | Rastreia até `order_item` |
| `unit_cost_cents` | INT | Para CMV |
| `client_uuid` | UUID | **Idempotência** |
| `is_synced` / `synced_at` | INT / TEXT | **Local** |
| `origin_device_id` | UUID | |
| `created_at` | TIMESTAMPTZ | |

---

### 2.3 Vendas

**`orders`**

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | UUIDv7 gerado no PDV |
| `tenant_id`, `store_id`, `device_id` | UUID | |
| `local_number` | INT | Sequência **por device** — nunca colide entre PDVs |
| `channel` | TEXT | `counter` \| `table` \| `delivery` \| `whatsapp` \| `qrcode` |
| `status` | TEXT | `open` \| `paid` \| `canceled` |
| `customer_id` | UUID NULL | |
| `subtotal_cents`, `discount_cents`, `total_cents` | INT | |
| `discount_tier_id` | UUID NULL | Diamante / Funcionário / Dono |
| `authorized_by_user_id` | UUID NULL | Quem liberou desconto acima do teto |
| `opened_at`, `closed_at` | TIMESTAMPTZ | |
| `client_uuid` | UUID | **`UNIQUE (tenant_id, client_uuid)`** |
| `is_synced`, `synced_at` | INT / TEXT | **Local** |
| `created_at`, `updated_at`, `origin_device_id` | | |

**`order_items`**

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `order_id` | UUID FK | |
| `product_id` | UUID FK | |
| `pricing_mode` | TEXT | `unit` \| `weight` |
| `qty` | NUMERIC(12,3) | Unidades, quando `unit` |
| `weight_grams` | INT NULL | **Peso líquido** lido da balança |
| `tare_grams` | INT | Tara aplicada |
| `unit_price_cents` | INT | Preço/kg quando pesado |
| `total_cents` | INT | |
| `scale_reading_raw` | TEXT NULL | **Quadro cru da balança** — prova anti-fraude |
| `canceled_at`, `canceled_by_user_id`, `cancel_reason` | | |
| `client_uuid`, `is_synced`, `synced_at` | | |

**`order_item_ingredients`** — foto imutável da baixa feita (ficha técnica muda
com o tempo; o que saiu do estoque naquele dia, não).
`id`, `order_item_id`, `inventory_item_id`, `consumed_mg`, `unit_cost_cents`.

**`payments`** — `id`, `order_id`, `method` (`cash` \| `debit` \| `credit` \|
`pix` \| `prepaid` \| `credit_account` \| `cashback`), `amount_cents`,
`change_cents`, `nsu`, `client_uuid`, `is_synced`, `synced_at`.

**`order_status_events`** (KDS) — `id`, `order_id`, `order_item_id` NULL,
`status` (`queued` \| `preparing` \| `ready` \| `delivered`), `station`,
`changed_by_user_id`, `created_at`, `client_uuid`, `is_synced`.

---

### 2.4 Salão

**`tables`** — `id`, `tenant_id`, `store_id`, `label`, `seats`, `status`, `area`.
**`tabs`** (comandas) — `id`, `table_id` NULL, `code` (cartão/comanda), `status`,
`opened_by_user_id`, `order_id`, `client_uuid`, `is_synced`.

---

### 2.5 Financeiro

**`customers`** — `id`, `tenant_id`, `name`, `phone`, `document`, `birthdate`,
`discount_tier_id`, `credit_limit_cents`, `is_blocked`, `updated_at`, `sync_version`.

**`discount_tiers`** — níveis automatizados.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID | |
| `code` | TEXT | `diamond` \| `employee` \| `owner` \| custom |
| `percent` | NUMERIC(5,2) | |
| `max_discount_cents` | INT | Teto por venda |
| `requires_manager_above_cents` | INT | Acima disso, exige senha de gerente |
| `auto_rule` | JSONB | Ex.: `{"min_spend_90d_cents": 200000}` → promoção automática |

**`customer_credit_ledger`** (pré-pago) — **ledger, nunca saldo mutável**.
`id`, `customer_id`, `entry_type` (`topup` \| `consume` \| `refund` \| `expire`),
`amount_cents` (sinalizado), `balance_after_cents`, `order_id` NULL,
`client_uuid`, `is_synced`.

**`cashback_ledger`** — `id`, `customer_id`, `entry_type` (`accrual` \| `redeem`
\| `expire`), `amount_cents`, `order_id`, `expires_at`, `client_uuid`, `is_synced`.

**`credit_accounts`** (fiado/pendura) — `id`, `customer_id`, `limit_cents`,
`current_balance_cents` (derivado/cacheado), `status` (`ok` \| `overdue` \| `blocked`).

**`credit_entries`** — `id`, `credit_account_id`, `entry_type` (`charge` \|
`payment`), `amount_cents`, `due_date`, `order_id`, `client_uuid`, `is_synced`.

---

### 2.6 Caixa e Anti-Furto

**`cash_sessions`** — a conciliação cega vive aqui.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `device_id`, `user_id` | UUID | |
| `opened_at`, `closed_at` | TIMESTAMPTZ | |
| `opening_amount_cents` | INT | |
| `declared_amount_cents` | INT NULL | **Contagem do operador — gravada primeiro** |
| `expected_amount_cents` | INT NULL | **Só calculado/exposto após `declared_*`** |
| `difference_cents` | INT NULL | Gerado pelo servidor |
| `blind_close` | BOOL | Sempre `true` em produção |
| `client_uuid`, `is_synced`, `synced_at` | | |

> Regra: enquanto `declared_amount_cents IS NULL`, nenhuma API retorna
> `expected_amount_cents`. A restrição é do backend, não da tela.

**`cash_movements`** — `id`, `cash_session_id`, `type` (`sale` \| `withdrawal` \|
`supply` \| `tip`), `amount_cents`, `reason`, `authorized_by_user_id`,
`client_uuid`, `is_synced`.

**`audit_ledger`** — **imutável, encadeado por hash**.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id`, `store_id`, `device_id` | UUID | |
| `seq` | INTEGER | Sequência **por device**, sem buraco. Gap = adulteração |
| `event_type` | TEXT | `item_canceled` \| `discount_applied` \| `drawer_opened` \| `price_override` \| `withdrawal` \| `weight_captured` \| `session_closed` |
| `severity` | TEXT | `info` \| `warning` \| `critical` |
| `actor_user_id` | UUID | Quem executou |
| `authorizer_user_id` | UUID NULL | Quem liberou (senha de gerente) |
| `payload` | JSONB / TEXT | **JSON canônico** (chaves ordenadas) |
| `prev_hash` | TEXT(64) | Hash da entrada anterior do mesmo device |
| `hash` | TEXT(64) | `SHA256(prev_hash ‖ seq ‖ event_type ‖ payload_canônico)` |
| `created_at` | TIMESTAMPTZ | |
| `client_uuid`, `is_synced`, `synced_at` | | Servidor **revalida a cadeia** no push |

---

### 2.6b Servidor Local (LAN — Fase 3)

**`edge_devices`** — celulares e telas pareados com **um** terminal.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id`, `store_id` | UUID | |
| `name` | TEXT | "Celular da Ana" — é como o caixa reconhece o aparelho |
| `kind` | TEXT | `waiter` \| `kds` |
| `token_hash` | TEXT | SHA-256. **Nunca o token em texto** |
| `operator_id` | UUID | garçom vinculado, quando houver |
| `paired_at`, `last_seen_at` | TEXT | `last_seen_at` é o que mostra quem está online |
| `revoked_at` | TEXT | celular perdido se revoga do caixa; vale no ato |

> **A LAN da loja não é confiável.** Na prática é a mesma rede do Wi-Fi que o
> restaurante oferece ao cliente, com a senha num cartaz. Estar na rede não
> autoriza nada: o aparelho precisa ter sido pareado por alguém com acesso
> **físico** ao caixa.

**`edge_pairing_codes`** — `code_hash` (PK), `created_at`, `expires_at`,
`used_at`, `used_by`. Seis dígitos, validade de 5 minutos, uso único. Curto
porque fica **visível na tela do caixa**, onde qualquer um que passe pelo balcão
consegue ler. O consumo é atômico (`UPDATE ... WHERE used_at IS NULL` conferindo
`rowcount`): ler-e-depois-gravar deixaria dois celulares usarem o mesmo código.

**`kds_tickets`** — a fila da cozinha.

| Campo | Tipo | Notas |
|---|---|---|
| `id` | UUID PK | |
| `order_id`, `order_item_id` | UUID FK | |
| `station` | TEXT | cozinha, bar, confeitaria |
| `product_name`, `quantity`, `notes` | TEXT | snapshot: "sem açúcar" pertence ao ticket |
| `status` | TEXT | `queued` → `preparing` → `ready` → `delivered` \| `canceled` |
| `queued_at` | TEXT | **origem do tempo**: o que importa é há quanto tempo o cliente pediu |
| `started_at`, `ready_at`, `delivered_at` | TEXT | limpos no recall — ver abaixo |

> **Por que uma tabela própria e não um campo em `order_items`.** O ciclo de
> vida da cozinha é outro: um item **pago** pode ainda não ter saído, e um item
> **pronto** pode voltar. O recall apaga os carimbos das etapas abandonadas; se
> `ready_at` sobrevivesse a um prato que voltou para a chapa, o relatório de
> tempo de preparo faria a cozinha parecer mais rápida justamente nos casos em
> que ela errou.

**`orders.channel` e `orders.origin_device_id` no salão.** Pedido do garçom
nasce com `channel = 'waiter'` e `origin_device_id` apontando para o **celular**,
não para o PDV onde está gravado. Manter a origem é o que permite ao relatório
dizer de qual aparelho saiu cada venda — e ao M09 separar o que veio do balcão
do que veio do salão.

---

### 2.7 Tabelas Locais do PDV (nunca sobem como entidade)

**`sync_outbox`** — a fila que garante o offline.

| Campo | Tipo | Notas |
|---|---|---|
| `seq` | INTEGER PK AUTOINCREMENT | Ordem de envio |
| `entity_table` | TEXT | `orders`, `stock_movements`… |
| `entity_id` | TEXT | |
| `client_uuid` | TEXT | Idempotência ponta a ponta |
| `operation` | TEXT | `insert` \| `update` \| `delete` |
| `payload_json` | TEXT | Snapshot da linha |
| `attempts` | INTEGER | Backoff exponencial |
| `last_error` | TEXT NULL | |
| `available_at` | TEXT | Próxima tentativa |
| `created_at` | TEXT | |

**`sync_cursors`** — `entity_table` PK, `last_server_seq`, `last_pulled_at`.
**`device_settings`** — porta COM da balança, protocolo, nome da impressora, tara padrão.

**`sync_inbox`** — comandos vindos do painel remoto (M15). Espelho do Outbox,
no sentido contrário.

| Campo | Tipo | Notas |
|---|---|---|
| `seq` | INTEGER PK AUTOINCREMENT | Ordem de aplicação |
| `command_uuid` | TEXT UNIQUE | **Idempotência**: reenvio não aplica duas vezes |
| `command_type` | TEXT | `apply_discount` \| `cancel_item` \| `update_order` \| `refresh_catalog` |
| `target_type`, `target_id` | TEXT | Pedido/item alvo |
| `payload_json` | TEXT | Parâmetros do comando |
| `issued_by_user_id` | TEXT | Quem emitiu **no painel** |
| `issued_at` | TEXT | Relógio do servidor (confiável) |
| `signature` | TEXT | Assinatura do servidor; o terminal não obedece sem verificar |
| `status` | TEXT | `pending` \| `applied` \| `rejected` \| `expired` |
| `rejection_reason` | TEXT NULL | Ex.: desconto acima do teto do perfil |
| `applied_at` | TEXT NULL | |
| `expires_at` | TEXT | Comando velho **não** é aplicado: um desconto emitido há 3 h para um pedido que já fechou não pode "acordar" depois |

> O resultado de cada comando sobe pelo `sync_outbox` como qualquer outro dado,
> junto da entrada de `audit_ledger` correspondente. O painel só mostra
> `aplicado` quando o terminal confirma — nunca por otimismo do servidor.

---

## 3. Fluxo de Sincronização (venda pesada)

```
[OFFLINE]                                    [ONLINE]
1. Balança → 0.847 kg estável
2. BEGIN TRANSACTION (SQLite)
     ├ INSERT order_items  (weight_grams=847, scale_reading_raw='...', is_synced=0)
     ├ INSERT stock_movements (qty_mg=-847000 ... por insumo)
     ├ INSERT order_item_ingredients
     ├ INSERT audit_ledger (weight_captured, hash encadeado)
     └ INSERT sync_outbox  × N
   COMMIT                                    ← tudo ou nada
3. ESC/POS → cupom + guilhotina + gaveta
                          ┌── conexão volta ──┐
4.                        │  POST /sync/push  │  lote ≤ 200, Idempotency-Key
5.                        │  Servidor: 1 TX   │  ON CONFLICT (tenant_id, client_uuid) DO NOTHING
6.                        │  ACK por item     │
7. UPDATE ... SET is_synced=1, synced_at=?, server_seq=?  ← só com ACK
```

Falha entre 4 e 7 → reenvio. O servidor responde `duplicate`, o cliente marca
como sincronizado do mesmo jeito. **Zero duplicidade, zero perda.**
