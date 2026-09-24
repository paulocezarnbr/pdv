/**
 * Aplicação idempotente do lote de sincronização — lado servidor.
 *
 * Este módulo é a metade nuvem da garantia "zero duplicidade, zero perda". O
 * cliente reenvia sempre que fica em dúvida; cabe aqui reconhecer a repetição.
 *
 * As quatro regras
 * ----------------
 *
 * 1. **Chave de idempotência é `(tenant_id, client_uuid)`.** Gerada no PDV,
 *    viaja com o dado e tem índice único. Um `INSERT ... ON CONFLICT DO
 *    NOTHING` transforma o reenvio em `duplicate` — que o cliente trata como
 *    sucesso.
 *
 * 2. **O lote inteiro é uma transação.** Falha do banco desfaz tudo e o
 *    terminal reenvia. Cada item roda num SAVEPOINT, porém: um payload que o
 *    banco recusa (NOT NULL, CHECK, tipo) vira `rejected` sozinho e vai para a
 *    quarentena do terminal, à vista. Antes ele derrubava o lote — e todos os
 *    seguintes, porque a fila anda em ordem —, e a loja parava de sincronizar
 *    por causa de uma linha.
 *
 * `update` é aplicado como UPDATE de verdade, com lista própria de colunas e
 * de dono (`UPDATABLE`) e idempotência pelo `client_uuid` da operação.
 *
 * 3. **A cadeia de auditoria é revalidada aqui.** O cliente afirma
 *    integridade; o servidor confere. Nunca se aceita o autoatestado de um
 *    banco que fica na máquina do caixa.
 *
 * 4. **Marca d'água alta por dispositivo.** Uma vez ancorado o `seq` N,
 *    qualquer tentativa de reenviar o `seq` N com conteúdo diferente é
 *    rejeitada e vira alerta de fraude. É isto que torna a venda sincronizada
 *    inalcançável para quem controla o PC da loja.
 */

import type { Tx } from "@/lib/db";
import { computeChainHash, hashesMatch } from "@/lib/crypto/audit";

export type ItemStatus = "applied" | "duplicate" | "rejected";

export interface SyncItem {
  entity_table: string;
  entity_id: string;
  client_uuid: string;
  operation: "insert" | "update" | "delete";
  payload: Record<string, unknown>;
}

export interface ItemResult {
  client_uuid: string;
  status: ItemStatus;
  message?: string;
  server_seq?: number;
}

/**
 * Tabelas que um terminal pode enviar, e as colunas que ele pode preencher.
 *
 * Lista fechada nas duas dimensões, e as duas importam. O **nome da tabela**
 * vem do cliente, que é território hostil: sem a lista, um terminal
 * comprometido escolheria onde escrever — `panel_users`, por exemplo. As
 * **colunas** vêm pelo mesmo caminho: sem a lista, bastaria mandar
 * `server_seq` ou `received_at` no payload para reescrever o cursor e o
 * carimbo do servidor, que são justamente o que não pode vir do cliente.
 */
const WRITABLE: Readonly<Record<string, readonly string[]>> = {
  orders: [
    "id", "store_id", "device_id", "client_uuid", "local_number", "channel",
    "status", "customer_id", "table_id", "operator_id", "served_by_user_id",
    "subtotal_cents", "discount_cents", "total_cents", "tip_cents",
    "opened_at", "closed_at", "bill_requested_at",
  ],
  order_items: [
    "id", "order_id", "client_uuid", "product_id", "product_name",
    "pricing_mode", "quantity", "net_weight_grams", "unit_price_cents",
    "total_cents", "scale_reading_raw", "canceled_at", "canceled_by_user_id",
    "cancel_reason", "created_by_user_id", "created_at",
  ],
  order_item_ingredients: [
    "id", "order_item_id", "client_uuid", "inventory_item_id",
    "inventory_item_name", "consumed_mg", "unit_cost_cents",
  ],
  payments: [
    "id", "order_id", "client_uuid", "method", "amount_cents", "change_cents",
    "nsu", "created_at",
  ],
  stock_movements: [
    "id", "store_id", "client_uuid", "inventory_item_id", "quantity_mg",
    "reason", "order_item_id", "created_at",
  ],
  cash_sessions: [
    "id", "store_id", "device_id", "client_uuid", "operator_id", "opened_at",
    "closed_at", "opening_cents", "closing_cents", "declared_cents",
    "expected_cents", "difference_cents", "blind_close",
  ],
  customers: [
    "id", "client_uuid", "name", "phone", "is_active", "created_at", "updated_at",
  ],
  cashback_ledger: [
    "id", "store_id", "client_uuid", "customer_id", "order_id", "entry_type",
    "amount_cents", "source_credit_id", "expires_at", "actor_user_id", "created_at",
  ],
  prepaid_ledger: [
    "id", "store_id", "client_uuid", "customer_id", "entry_type",
    "amount_cents", "order_id", "actor_user_id", "authorizer_user_id", "created_at",
  ],
  customer_credit_accounts: [
    "customer_id", "client_uuid", "limit_cents", "due_days", "is_active", "updated_at",
  ],
  credit_account_ledger: [
    "id", "store_id", "client_uuid", "customer_id", "entry_type", "amount_cents",
    "order_id", "source_charge_id", "due_at", "actor_user_id",
    "authorizer_user_id", "created_at",
  ],
  discount_tiers: [
    "id", "store_id", "client_uuid", "code", "name", "percent_basis_points",
    "priority", "requires_manager", "valid_from", "valid_until", "is_active", "updated_at",
  ],
  customer_discount_tiers: [
    "customer_id", "client_uuid", "tier_id", "assigned_by_user_id", "assigned_at",
  ],
  store_tables: [
    "id", "store_id", "client_uuid", "label", "area", "seats", "sort_order",
    "is_active",
  ],
  audit_ledger: [
    "id", "store_id", "device_id", "client_uuid", "seq", "event_type",
    "severity", "actor_user_id", "authorizer_user_id", "payload_json",
    "prev_hash", "hash", "created_at",
  ],
};

export const WRITABLE_TABLES = Object.freeze(Object.keys(WRITABLE));

/**
 * O que um `update` do terminal pode mudar, e em qual linha.
 *
 * Mais estreito que `WRITABLE`, e de propósito: quem cria a linha preenche o
 * registro inteiro, mas quem a altera só mexe no que muda de verdade na vida
 * da entidade. `local_number`, `device_id`, `opened_at` e o preço de um item
 * lançado não mudam — um terminal que tentasse mudá-los estaria reescrevendo a
 * venda, e isso não é atualização, é adulteração.
 *
 * `scope` diz de quem é a linha:
 *
 * * `own_open_order` — pedido **deste** terminal e ainda aberto. Fechado,
 *   corrige-se por estorno, nunca por edição; e o caixa de uma loja não
 *   reescreve a venda de outro caixa.
 * * `item_of_own_open_order` — item de um pedido assim, e o pedido de destino
 *   (quando o item muda de comanda) precisa ser assim também.
 * * `store` — cadastro desta loja.
 * * `tenant` — cadastro do tenant (o RLS e o `WHERE tenant_id` bastam).
 */
type UpdateScope = "own_open_order" | "item_of_own_open_order" | "store" | "tenant";

interface UpdateRule {
  key: string;
  columns: readonly string[];
  scope: UpdateScope;
}

const UPDATABLE: Readonly<Record<string, UpdateRule>> = {
  orders: {
    key: "id",
    columns: [
      "status", "customer_id", "table_id", "served_by_user_id",
      "subtotal_cents", "discount_cents", "total_cents", "tip_cents",
      "closed_at", "bill_requested_at",
    ],
    scope: "own_open_order",
  },
  order_items: {
    key: "id",
    columns: ["order_id", "canceled_at", "canceled_by_user_id", "cancel_reason"],
    scope: "item_of_own_open_order",
  },
  discount_tiers: {
    key: "id",
    columns: [
      "name", "percent_basis_points", "priority", "requires_manager",
      "valid_from", "valid_until", "is_active", "updated_at",
    ],
    scope: "store",
  },
  customer_credit_accounts: {
    key: "customer_id",
    columns: ["limit_cents", "due_days", "is_active", "updated_at"],
    scope: "tenant",
  },
  store_tables: {
    key: "id",
    columns: ["label", "area", "seats", "sort_order", "is_active", "updated_at"],
    scope: "store",
  },
  customer_discount_tiers: {
    key: "customer_id",
    columns: ["tier_id", "assigned_by_user_id", "assigned_at"],
    scope: "tenant",
  },
};

/**
 * Erro do Postgres que é **do dado**, e não do banco.
 *
 * Classe 22 (dado inválido: texto onde vai número, data impossível) e 23
 * (restrição: NOT NULL, chave, CHECK, gatilho de proteção). Reenviar o mesmo
 * payload dá o mesmo erro — então é recusa, e o item vai para a quarentena do
 * terminal. Qualquer outro erro (conexão, serialização, disco) é do banco, e
 * derruba o lote para o terminal tentar de novo.
 */
function isDataError(error: unknown): error is { code: string; message: string } {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && /^2[23]/.test(code);
}

export interface MergeContext {
  tenantId: string;
  storeId: string;
  deviceId: string;
  /** O segredo HMAC do ledger daquele terminal. */
  secret: Buffer;
}

export class SyncMerger {
  constructor(private readonly context: MergeContext) {}

  /**
   * Aplica o lote inteiro. O chamador garante a transação única.
   *
   * A ordem importa: entradas de auditoria precisam ser aplicadas na sequência
   * em que foram criadas, ou a validação da cadeia falha por motivo legítimo.
   * O cliente já envia em ordem (o outbox é uma fila por `seq`); ordenar de
   * novo aqui protege contra um lote remontado por um retry parcial.
   */
  async apply(items: SyncItem[], tx: Tx): Promise<ItemResult[]> {
    const ordered = [...items].sort(auditFirstBySeq);
    const results: ItemResult[] = [];

    for (const item of ordered) {
      if (!(item.entity_table in WRITABLE)) {
        results.push({
          client_uuid: item.client_uuid,
          status: "rejected",
          message: `tabela não gravável por terminal: ${item.entity_table}`,
        });
        continue;
      }

      results.push(await this.applyIsolated(item, tx));
    }

    // Devolve na ordem em que o cliente enviou: ele casa resultado com item
    // pelo `client_uuid`, mas uma resposta reordenada dificultaria a leitura
    // de um log de suporte lado a lado com o pedido.
    const byUuid = new Map(results.map((r) => [r.client_uuid, r]));
    return items.map(
      (item) =>
        byUuid.get(item.client_uuid) ?? {
          client_uuid: item.client_uuid,
          status: "rejected" as const,
          message: "item sem resultado",
        },
    );
  }

  /**
   * Um item, dentro de um SAVEPOINT.
   *
   * Sem isto, um único payload inválido derrubava o lote inteiro — e com ele
   * todo lote seguinte, porque o terminal reenvia em ordem: a loja parava de
   * sincronizar por causa de uma linha. Foi exatamente o que aconteceu com o
   * `update` de pedido, que batia no NOT NULL de `local_number` a cada mesa
   * fechada.
   *
   * Erro do dado vira `rejected` (quarentena no terminal, visível, esperando
   * gente). Erro do banco continua derrubando o lote: esse o reenvio resolve.
   */
  private async applyIsolated(item: SyncItem, tx: Tx): Promise<ItemResult> {
    try {
      return await tx.savepoint((sp) =>
        item.entity_table === "audit_ledger"
          ? this.applyAuditEntry(item, sp)
          : item.operation === "update"
            ? this.applyUpdate(item, sp)
            : item.operation === "insert"
              ? this.applyGeneric(item, sp)
              : Promise.resolve(reject(item, `operação não suportada: ${item.operation}`)),
      );
    } catch (error) {
      if (isDataError(error)) {
        return reject(item, `dado recusado pelo banco (${error.code}): ${error.message}`);
      }
      throw error;
    }
  }

  // -- atualização --------------------------------------------------------- //

  /**
   * Aplica um `update`: só as colunas de `UPDATABLE`, só na linha que o
   * terminal pode alterar, e uma vez só.
   *
   * A idempotência é pelo `client_uuid` da operação, em
   * `sync_applied_updates`. O lote reenviado depois de uma resposta perdida
   * volta como `duplicate` em vez de reaplicar mudança velha por cima de nova.
   */
  private async applyUpdate(item: SyncItem, tx: Tx): Promise<ItemResult> {
    const rule = UPDATABLE[item.entity_table];
    if (!rule) {
      return reject(item, `atualização não aceita em ${item.entity_table}`);
    }
    const { tenantId, storeId, deviceId } = this.context;

    const seen = await tx`
      SELECT 1 FROM sync_applied_updates
       WHERE tenant_id = ${tenantId} AND client_uuid = ${item.client_uuid}
    `;
    if (seen.length) return { client_uuid: item.client_uuid, status: "duplicate" };

    const key = String(item.payload[rule.key] ?? item.entity_id);
    const changes: Record<string, unknown> = {};
    for (const column of rule.columns) {
      if (Object.hasOwn(item.payload, column)) {
        changes[column] = normalize(item.payload[column]);
      }
    }

    const refusal = await this.updateRefusal(rule, key, changes, tx);
    if (refusal) return reject(item, refusal);

    const columns = Object.keys(changes).sort();
    if (columns.length) {
      const params: unknown[] = [...columns.map((c) => changes[c]), tenantId, key];
      let where = `tenant_id = $${columns.length + 1} AND "${rule.key}"::text = $${columns.length + 2}`;
      if (rule.scope === "store") {
        params.push(storeId);
        where += ` AND store_id = $${params.length}`;
      }
      // O `server_seq` anda: quem lê por cursor precisa enxergar a mudança.
      const updated = await tx.unsafe(
        `UPDATE ${item.entity_table}
            SET ${columns.map((c, i) => `"${c}" = $${i + 1}`).join(", ")},
                server_seq = nextval('server_seq_global')
          WHERE ${where}
          RETURNING 1`,
        params as never[],
      );
      if (!updated.length) {
        return reject(item, `${item.entity_table} ${key} não existe nesta loja`);
      }
    }

    await tx`
      INSERT INTO sync_applied_updates (tenant_id, client_uuid, device_id, entity_table, entity_id)
      VALUES (${tenantId}, ${item.client_uuid}, ${deviceId}, ${item.entity_table}, ${key})
    `;
    return { client_uuid: item.client_uuid, status: "applied" };
  }

  /**
   * Por que o terminal NÃO pode aplicar esta atualização — ou `null`.
   *
   * A mensagem vai para a quarentena do terminal e para o suporte, então diz o
   * motivo em vez de só "recusado".
   */
  private async updateRefusal(
    rule: UpdateRule,
    key: string,
    changes: Record<string, unknown>,
    tx: Tx,
  ): Promise<string | null> {
    const { tenantId, deviceId } = this.context;

    const ownOpenOrder = async (orderId: string, label: string): Promise<string | null> => {
      const [order] = await tx<{ device_id: string; status: string }[]>`
        SELECT device_id::text AS device_id, status FROM orders
         WHERE tenant_id = ${tenantId} AND id::text = ${orderId}
      `;
      if (!order) return `${label} ${orderId} ainda não chegou à nuvem`;
      if (order.device_id !== deviceId) return `${label} ${orderId} é de outro terminal`;
      if (order.status !== "open") {
        return `${label} ${orderId} já está ${order.status}: pedido fechado não se edita`;
      }
      return null;
    };

    if (rule.scope === "own_open_order") {
      return ownOpenOrder(key, "pedido");
    }
    if (rule.scope === "item_of_own_open_order") {
      const [row] = await tx<{ order_id: string }[]>`
        SELECT order_id::text AS order_id FROM order_items
         WHERE tenant_id = ${tenantId} AND id::text = ${key}
      `;
      if (!row) return `item ${key} ainda não chegou à nuvem`;
      const origin = await ownOpenOrder(row.order_id, "pedido do item");
      if (origin) return origin;
      if (changes["order_id"] !== undefined && String(changes["order_id"]) !== row.order_id) {
        return ownOpenOrder(String(changes["order_id"]), "pedido de destino");
      }
    }
    return null;
  }

  // -- auditoria ----------------------------------------------------------- //

  private async applyAuditEntry(item: SyncItem, tx: Tx): Promise<ItemResult> {
    const payload = item.payload;
    const seq = Number(payload["seq"]);
    const digest = String(payload["hash"] ?? "");
    const { tenantId, deviceId } = this.context;

    if (!Number.isInteger(seq) || seq < 1 || !digest) {
      return reject(item, "entrada de auditoria malformada");
    }

    const anchored = await tx<{ last_seq: string; last_hash: string }[]>`
      SELECT last_seq, last_hash FROM device_anchors
       WHERE tenant_id = ${tenantId} AND device_id = ${deviceId}
    `;
    const anchor = anchored[0];

    // --- Regra 4: marca d'água alta ---------------------------------------
    if (anchor) {
      const lastSeq = Number(anchor.last_seq);

      if (seq <= lastSeq) {
        // Reenvio de algo já ancorado. Se o hash bate, é só uma resposta
        // perdida — duplicata benigna. Se NÃO bate, alguém reescreveu história
        // já registrada na nuvem.
        const existing = await tx<{ hash: string }[]>`
          SELECT hash FROM audit_ledger
           WHERE tenant_id = ${tenantId} AND device_id = ${deviceId}
             AND seq = ${seq}
        `;
        const found = existing[0];
        if (found && !hashesMatch(found.hash, digest)) {
          await this.raiseAlert(
            tx,
            `Reescrita de auditoria já ancorada no seq ${seq}`,
            {
              seq,
              servidor: found.hash.slice(0, 16),
              terminal: digest.slice(0, 16),
            },
          );
          return reject(item, "seq já ancorado com conteúdo divergente");
        }
        return { client_uuid: item.client_uuid, status: "duplicate" };
      }

      if (seq !== lastSeq + 1) {
        // Buraco: o terminal pulou entradas. Ou houve perda, ou alguém apagou
        // o rastro local antes de sincronizar.
        await this.raiseAlert(tx, `Buraco na auditoria`, {
          esperado: lastSeq + 1,
          recebido: seq,
        });
        return reject(item, `seq fora de ordem (esperado ${lastSeq + 1})`);
      }

      if (String(payload["prev_hash"] ?? "") !== anchor.last_hash) {
        await this.raiseAlert(tx, `Elo quebrado no seq ${seq}`, {
          seq,
          prev_hash_recebido: String(payload["prev_hash"] ?? "").slice(0, 16),
          ultimo_ancorado: anchor.last_hash.slice(0, 16),
        });
        return reject(item, "prev_hash divergente");
      }
    }

    // --- Regra 3: recalcular o HMAC ---------------------------------------
    const expected = computeChainHash(this.context.secret, {
      prevHash: String(payload["prev_hash"] ?? ""),
      seq,
      eventType: String(payload["event_type"] ?? ""),
      payloadJson: String(payload["payload_json"] ?? ""),
      createdAt: String(payload["created_at"] ?? ""),
    });

    if (!hashesMatch(expected, digest)) {
      await this.raiseAlert(tx, `HMAC inválido no seq ${seq}`, {
        seq,
        motivo: "conteúdo adulterado ou chave errada",
      });
      return reject(item, "HMAC da cadeia inválido");
    }

    const serverSeq = await this.insertIdempotent(item, tx);
    if (serverSeq === null) {
      return { client_uuid: item.client_uuid, status: "duplicate" };
    }

    await tx`
      INSERT INTO device_anchors (tenant_id, device_id, last_seq, last_hash)
      VALUES (${tenantId}, ${deviceId}, ${seq}, ${digest})
      ON CONFLICT (tenant_id, device_id) DO UPDATE
         SET last_seq = EXCLUDED.last_seq,
             last_hash = EXCLUDED.last_hash,
             updated_at = now()
    `;

    return { client_uuid: item.client_uuid, status: "applied", server_seq: serverSeq };
  }

  // -- demais entidades ---------------------------------------------------- //

  private async applyGeneric(item: SyncItem, tx: Tx): Promise<ItemResult> {
    const serverSeq = await this.insertIdempotent(item, tx);
    return serverSeq === null
      ? { client_uuid: item.client_uuid, status: "duplicate" }
      : { client_uuid: item.client_uuid, status: "applied", server_seq: serverSeq };
  }

  /**
   * `INSERT ... ON CONFLICT DO NOTHING`. Devolve o `server_seq` se gravou
   * agora, ou `null` se já existia.
   *
   * Regra 1 em uma instrução. O índice único `(tenant_id, client_uuid)` é o
   * que dá sentido ao `ON CONFLICT`; sem ele o reenvio duplicaria a venda.
   *
   * `tenant_id` e `store_id` vêm do **contexto**, nunca do payload: aceitar os
   * do corpo permitiria a um terminal gravar dados no tenant de outro
   * restaurante. As demais colunas são filtradas contra a lista branca — o que
   * não está lá é descartado em silêncio, e não é erro: um terminal mais novo
   * que a nuvem manda colunas que ela ainda não conhece, e recusar o lote
   * inteiro por isso pararia a loja por causa de um campo novo.
   */
  private async insertIdempotent(item: SyncItem, tx: Tx): Promise<number | null> {
    const allowed = WRITABLE[item.entity_table];
    if (!allowed) return null;

    const row: Record<string, unknown> = {
      tenant_id: this.context.tenantId,
    };
    if (allowed.includes("store_id")) row["store_id"] = this.context.storeId;
    if (allowed.includes("device_id")) row["device_id"] = this.context.deviceId;

    for (const column of allowed) {
      if (column === "store_id" || column === "device_id") continue;
      if (Object.hasOwn(item.payload, column)) {
        row[column] = normalize(item.payload[column]);
      }
    }
    row["client_uuid"] = item.client_uuid;
    if (allowed.includes("id") && !row["id"]) row["id"] = item.entity_id;

    const columns = Object.keys(row).sort();
    const values = columns.map((column) => row[column]);

    // `tx(...)` com identificadores: o driver faz o escape de nome de coluna e
    // parametriza os valores. O nome da TABELA passa pela lista branca acima,
    // então a interpolação dele não é entrada do usuário.
    const inserted = await tx.unsafe<{ server_seq: string }[]>(
      `INSERT INTO ${item.entity_table} (${columns.map((c) => `"${c}"`).join(", ")})
       VALUES (${columns.map((_, i) => `$${i + 1}`).join(", ")})
       ON CONFLICT (tenant_id, client_uuid) DO NOTHING
       RETURNING server_seq`,
      values as never[],
    );

    const first = inserted[0];
    return first ? Number(first.server_seq) : null;
  }

  private async raiseAlert(
    tx: Tx,
    reason: string,
    detail: Record<string, unknown>,
  ): Promise<void> {
    await tx`
      INSERT INTO fraud_alerts (tenant_id, store_id, device_id, reason, detail)
      VALUES (${this.context.tenantId}, ${this.context.storeId},
              ${this.context.deviceId}, ${reason}, ${tx.json(detail as never)})
    `;
    console.warn("[fraude]", {
      tenant: this.context.tenantId,
      device: this.context.deviceId,
      reason,
      detail,
    });
  }
}

function reject(item: SyncItem, message: string): ItemResult {
  return { client_uuid: item.client_uuid, status: "rejected", message };
}

/**
 * Auditoria primeiro, e em ordem de `seq`.
 *
 * O resto do lote pode entrar em qualquer ordem — são inserções independentes
 * dedupadas por `client_uuid`. A auditoria não: o elo N só valida depois do
 * N-1 estar ancorado, e um lote remontado fora de ordem faria a cadeia ser
 * rejeitada por um motivo que não é adulteração nenhuma.
 */
function auditFirstBySeq(a: SyncItem, b: SyncItem): number {
  const aAudit = a.entity_table === "audit_ledger";
  const bAudit = b.entity_table === "audit_ledger";
  if (aAudit && bAudit) {
    return Number(a.payload["seq"] ?? 0) - Number(b.payload["seq"] ?? 0);
  }
  if (aAudit) return -1;
  if (bAudit) return 1;
  return 0;
}

/**
 * Converte o que o JSON entrega para o que o Postgres aceita.
 *
 * O terminal manda inteiro como inteiro e data como string ISO-8601, que o
 * driver resolve sozinho. O que não resolve é objeto aninhado numa coluna de
 * texto — vira `[object Object]` e a informação some sem erro nenhum.
 */
function normalize(value: unknown): unknown {
  if (value !== null && typeof value === "object" && !(value instanceof Date)) {
    return JSON.stringify(value);
  }
  return value;
}
