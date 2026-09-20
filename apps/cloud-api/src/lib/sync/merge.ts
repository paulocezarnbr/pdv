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
 * 2. **O lote inteiro é uma transação.** Ou entra tudo, ou nada. Aplicação
 *    parcial deixaria o item de venda gravado sem o movimento de estoque
 *    correspondente, e o CMV do tenant passaria a mentir em silêncio.
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
  audit_ledger: [
    "id", "store_id", "device_id", "client_uuid", "seq", "event_type",
    "severity", "actor_user_id", "authorizer_user_id", "payload_json",
    "prev_hash", "hash", "created_at",
  ],
};

export const WRITABLE_TABLES = Object.freeze(Object.keys(WRITABLE));

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

      results.push(
        item.entity_table === "audit_ledger"
          ? await this.applyAuditEntry(item, tx)
          : await this.applyGeneric(item, tx),
      );
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
