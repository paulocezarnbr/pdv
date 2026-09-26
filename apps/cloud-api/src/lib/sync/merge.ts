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

import { createHash } from "node:crypto";

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
    // Schema 15 do caixa: o cadastro completo (migração 020).
    "email", "cpf", "is_resident", "unit_block", "unit_number", "birth_date",
    "marketing_opt_in", "marketing_opt_in_at",
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
    "is_active", "updated_at",
  ],
  audit_ledger: [
    "id", "store_id", "device_id", "client_uuid", "seq", "event_type",
    "severity", "actor_user_id", "authorizer_user_id", "payload_json",
    "prev_hash", "hash", "created_at",
  ],
};

export const WRITABLE_TABLES = Object.freeze(Object.keys(WRITABLE));

type Payload = Record<string, unknown>;

/**
 * O formato que os caixas mandam, traduzido para o daqui.
 *
 * A tradução mora na nuvem, e não no caixa, por um motivo só: os caixas já
 * instalados têm a fila cheia neste formato. Corrigir lá deixaria essa fila
 * presa para sempre; corrigir aqui a esvazia no próximo ciclo, sem reinstalar
 * nada. `contracts/push-day-1.1.2.json` é a fila de um caixa desses, e o teste
 * de contrato a aplica inteira.
 *
 * Nunca inventa valor: só renomeia o que veio. O que não veio fica nulo.
 */
const ADAPTERS: Readonly<Record<string, (payload: Payload) => Payload>> = {
  stock_movements: (payload) => {
    const adapted = { ...payload };
    adapted["quantity_mg"] ??= payload["qty_mg"];
    adapted["reason"] ??= payload["movement_type"];
    const reference = String(payload["reference_type"] ?? "");
    if (reference.startsWith("order_item")) adapted["order_item_id"] ??= payload["reference_id"];
    return adapted;
  },
  // A comanda de mesa até a 1.1.3 mandava o rótulo só como `table_label`, que
  // não é coluna daqui: toda mesa chegava sem nome no painel, a não ser que
  // fosse trocada de mesa depois. O rótulo mora em `customer_id` (a cópia que o
  // cupom imprimiu), dos dois lados.
  orders: (payload) => {
    const adapted = { ...payload };
    if (payload["table_label"] !== undefined) adapted["customer_id"] ??= payload["table_label"];
    return adapted;
  },
};

/**
 * O que um `update` do caixa pode mudar, e como.
 *
 * O `insert` é idempotente por `client_uuid`, e isso não serve para alteração:
 * os cadastros do caixa (nível de desconto, limite de fiado, mesa) reusam o
 * mesmo `client_uuid` ao mudar, e o `ON CONFLICT DO NOTHING` descartava a
 * mudança calado — o desconto de 15% continuava 10% aqui para sempre. As
 * mudanças de comanda usam `client_uuid` novo, e o insert batia na chave
 * primária e derrubava o lote.
 *
 * * `key` — a coluna que identifica o registro no caixa;
 * * `columns` — lista branca, como no insert;
 * * `newer` — coluna de data que decide quem é mais novo: uma mudança antiga
 *   que chega depois (reenvio de lote perdido) não desfaz a recente;
 * * `upsert` — cadastro: se ainda não existe aqui (o insert foi perdido ou
 *   está em quarentena), a mudança cria. Movimento (comanda, item) nunca cria:
 *   um "pago" sem a venda seria faturamento sem venda.
 */
interface Mutable {
  key: string;
  columns: readonly string[];
  newer?: string;
  upsert?: boolean;
}

const UPDATABLE: Readonly<Record<string, Mutable>> = {
  orders: {
    key: "id",
    columns: [
      "status", "closed_at", "subtotal_cents", "discount_cents", "total_cents",
      "tip_cents", "table_id", "served_by_user_id", "bill_requested_at",
      "customer_id", "operator_id", "opened_at", "local_number",
    ],
  },
  order_items: {
    key: "id",
    // `order_id`: dividir e juntar conta no caixa mudam o item de comanda. A
    // mudança passa por `itemMoveRefusal` antes — nunca é aplicada às cegas.
    columns: ["order_id", "canceled_at", "canceled_by_user_id", "cancel_reason"],
  },
  discount_tiers: {
    key: "id",
    columns: [
      "code", "name", "percent_basis_points", "priority", "requires_manager",
      "valid_from", "valid_until", "is_active", "updated_at",
    ],
    newer: "updated_at",
    upsert: true,
  },
  customer_discount_tiers: {
    key: "customer_id",
    columns: ["tier_id", "assigned_by_user_id", "assigned_at"],
    newer: "assigned_at",
    upsert: true,
  },
  customer_credit_accounts: {
    key: "customer_id",
    columns: ["limit_cents", "due_days", "is_active", "updated_at"],
    newer: "updated_at",
    upsert: true,
  },
  store_tables: {
    key: "id",
    columns: ["label", "area", "seats", "sort_order", "is_active", "updated_at"],
    newer: "updated_at",
    upsert: true,
  },
  // O caixa corrige o cadastro (apartamento, e-mail, consentimento). A mudança
  // mais nova vence; o `created_at` não muda nunca.
  customers: {
    key: "id",
    columns: [
      "name", "phone", "email", "cpf", "is_resident", "unit_block", "unit_number",
      "birth_date", "marketing_opt_in", "marketing_opt_in_at", "is_active", "updated_at",
    ],
    newer: "updated_at",
    upsert: true,
  },
};

/** Estados de comanda que não voltam: pago é dinheiro, cancelado é trilha. */
const FINAL_ORDER_STATUS = new Set(["paid", "canceled"]);

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

      if (item.operation === "delete") {
        // O caixa não apaga nada na nuvem: cancelar é marcar, e a marca vem
        // como `update`. Um `delete` aqui é terminal fora do contrato.
        results.push(reject(item, "o terminal não apaga registros na nuvem"));
        continue;
      }

      const adapted = adapt(item);
      if (adapted.entity_table === "audit_ledger") {
        results.push(await this.applyAuditEntry(adapted, tx));
      } else if (adapted.entity_table === "customers") {
        results.push(await this.applyCustomer(adapted, tx));
      } else if (adapted.operation === "update" && adapted.entity_table in UPDATABLE) {
        results.push(await this.applyUpdate(adapted, tx));
      } else {
        results.push(await this.applyGeneric(adapted, tx));
      }
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

  // -- cadastro de cliente ------------------------------------------------- //

  /**
   * O cliente, com a colisão de WhatsApp contida neste item.
   *
   * Os clientes não descem da nuvem para os outros caixas: a mesma pessoa
   * cadastrada em dois balcões chega aqui duas vezes, com o mesmo WhatsApp, e
   * o índice único recusa a segunda. Sem o savepoint, essa recusa derrubava o
   * lote inteiro — e com ele as vendas do caixa, que ficavam presas atrás de
   * um cadastro repetido.
   */
  private async applyCustomer(item: SyncItem, tx: Tx): Promise<ItemResult> {
    try {
      return await tx.savepoint((sp) =>
        item.operation === "update" && item.entity_table in UPDATABLE
          ? this.applyUpdate(item, sp as never)
          : this.applyGeneric(item, sp as never),
      ) as ItemResult;
    } catch (error) {
      if ((error as { code?: string }).code !== "23505") throw error;
      return reject(item, "este WhatsApp já está no cadastro de outro cliente, feito em outro caixa");
    }
  }

  // -- demais entidades ---------------------------------------------------- //

  private async applyGeneric(item: SyncItem, tx: Tx): Promise<ItemResult> {
    if (item.entity_table === "order_items") await this.fillProductName(item, tx);
    const serverSeq = await this.insertIdempotent(item, tx);
    if (item.entity_table === "order_items") await this.applyIngredients(item, tx);
    return serverSeq === null
      ? { client_uuid: item.client_uuid, status: "duplicate" }
      : { client_uuid: item.client_uuid, status: "applied", server_seq: serverSeq };
  }

  /**
   * Os insumos consumidos pelo item viajam DENTRO dele, em `ingredients`.
   *
   * A nuvem descartava a chave em silêncio (não está na lista branca), e o CMV
   * do painel era sempre zero. Cada insumo vira uma linha de
   * `order_item_ingredients`, na mesma transação do item. A idempotência é a
   * do resto: `client_uuid` do próprio insumo quando o caixa manda, senão um
   * derivado estável do item — o reenvio cai no mesmo `ON CONFLICT`.
   *
   * Roda também quando o item é duplicata: é barato, e é o que recupera um
   * item gravado antes desta correção, quando os insumos ainda eram jogados
   * fora.
   */
  private async applyIngredients(item: SyncItem, tx: Tx): Promise<void> {
    const list = item.payload["ingredients"];
    if (!Array.isArray(list)) return;
    const orderItemId = String(item.payload["id"] ?? item.entity_id);

    for (const [index, raw] of list.entries()) {
      if (raw === null || typeof raw !== "object") continue;
      const ingredient = raw as Payload;
      const seed = `${item.client_uuid}:insumo:${index}`;
      const clientUuid = text(ingredient["client_uuid"]) ?? derivedUuid(seed);
      const id = text(ingredient["id"]) ?? derivedUuid(`${seed}:id`);
      await this.insertIdempotent(
        {
          entity_table: "order_item_ingredients",
          entity_id: id,
          client_uuid: clientUuid,
          operation: "insert",
          payload: {
            id,
            order_item_id: orderItemId,
            inventory_item_id: ingredient["inventory_item_id"],
            inventory_item_name: ingredient["inventory_item_name"] ?? "",
            consumed_mg: ingredient["consumed_mg"],
            unit_cost_cents: ingredient["unit_cost_cents"] ?? 0,
          },
        },
        tx,
      );
    }
  }

  /**
   * Item lançado pelo garçom num caixa até a 1.1.2 vem sem nome. O nome é o
   * que o painel agrupa ("mais vendidos"); sem ele, todo item de mesa caía num
   * grupo em branco. O produto é do cadastro daqui, então o nome sai dele — o
   * preço não: preço é do momento da venda, e o de hoje não é o daquele dia.
   */
  private async fillProductName(item: SyncItem, tx: Tx): Promise<void> {
    const productId = text(item.payload["product_id"]);
    if (!productId || text(item.payload["product_name"])) return;
    const [product] = await tx<{ name: string }[]>`
      SELECT name FROM products
       WHERE tenant_id = ${this.context.tenantId} AND id::text = ${productId}
    `;
    if (product) item.payload["product_name"] = product.name;
  }

  // -- alterações ---------------------------------------------------------- //

  /**
   * Aplica um `update` do caixa, com as travas de cada tabela.
   *
   * O tenant entra no `WHERE` além do RLS: um terminal que mande o id de um
   * registro de outro restaurante não acha nada, e, sendo cadastro, a recusa
   * vem antes da criação — criar aqui deixaria um terminal clonado descobrir,
   * por tentativa, quais ids existem lá.
   */
  private async applyUpdate(item: SyncItem, tx: Tx): Promise<ItemResult> {
    const rule = UPDATABLE[item.entity_table]!;
    const table = item.entity_table;
    const keyValue = text(item.payload[rule.key]) ?? item.entity_id;

    const changes: Payload = {};
    for (const column of rule.columns) {
      if (Object.hasOwn(item.payload, column)) changes[column] = normalize(item.payload[column]);
    }

    const [current] = await tx.unsafe<{
      status?: string; canceled_at?: unknown; order_id?: string; stale?: boolean;
    }[]>(
      `SELECT ${table === "orders" ? "status," : ""}
              ${table === "order_items" ? "canceled_at, order_id::text AS order_id," : ""}
              ${rule.newer && changes[rule.newer] !== undefined
                ? `("${rule.newer}" IS NOT NULL AND "${rule.newer}" > $3::timestamptz) AS stale`
                : "false AS stale"}
         FROM ${table}
        WHERE tenant_id = $1 AND "${rule.key}"::text = $2
        FOR UPDATE`,
      [this.context.tenantId, keyValue, ...(rule.newer && changes[rule.newer] !== undefined
        ? [changes[rule.newer]] : [])] as never[],
    );

    if (!current) {
      if (!rule.upsert) return reject(item, "o registro ainda não chegou à nuvem");
      return this.createFromUpdate(item, tx);
    }

    // Mudança mais velha que o que já está aqui: um lote antigo reenviado.
    if (current.stale) return { client_uuid: item.client_uuid, status: "duplicate" };

    if (table === "orders" && changes["status"] !== undefined) {
      const status = String(current.status ?? "");
      if (FINAL_ORDER_STATUS.has(status) && status !== changes["status"]) {
        return reject(item, `comanda já ${status === "paid" ? "paga" : "cancelada"} não muda de estado`);
      }
    }
    if (table === "order_items") {
      // O primeiro cancelamento é o que vale: é ele que tem quem autorizou.
      if (current.canceled_at) return { client_uuid: item.client_uuid, status: "duplicate" };
      if (changes["order_id"] !== undefined) {
        const target = String(changes["order_id"]);
        // Já está lá: é o reenvio de uma mudança aplicada.
        if (target === current.order_id) {
          delete changes["order_id"];
        } else {
          const refusal = await this.itemMoveRefusal(String(current.order_id), target, tx);
          if (refusal) return reject(item, refusal);
        }
      }
    }

    const columns = Object.keys(changes).sort();
    if (columns.length === 0) return { client_uuid: item.client_uuid, status: "duplicate" };
    const assignments = columns.map((column, index) => `"${column}" = $${index + 3}`);
    const [updated] = await tx.unsafe<{ server_seq: string }[]>(
      `UPDATE ${table}
          SET ${assignments.join(", ")}, server_seq = nextval('server_seq_global')
        WHERE tenant_id = $1 AND "${rule.key}"::text = $2
        RETURNING server_seq`,
      [this.context.tenantId, keyValue, ...columns.map((c) => changes[c])] as never[],
    );
    return {
      client_uuid: item.client_uuid,
      status: "applied",
      server_seq: Number(updated!.server_seq),
    };
  }

  /**
   * Por que o item NÃO pode mudar de comanda — ou `null`.
   *
   * As duas pontas precisam estar abertas, neste restaurante e neste
   * terminal. Comanda fechada é dinheiro recebido; mover item para ou de uma
   * delas reescreveria uma venda já paga. E o caixa de uma loja não mexe na
   * comanda que outro caixa abriu.
   */
  private async itemMoveRefusal(from: string, to: string, tx: Tx): Promise<string | null> {
    const rows = await tx<{ id: string; status: string; device_id: string }[]>`
      SELECT id::text AS id, status, device_id::text AS device_id FROM orders
       WHERE tenant_id = ${this.context.tenantId} AND id::text IN (${from}, ${to})
    `;
    for (const [id, label] of [[from, "origem"], [to, "destino"]] as const) {
      const order = rows.find((row) => row.id === id);
      if (!order) return `comanda de ${label} ainda não chegou à nuvem`;
      if (order.device_id !== this.context.deviceId) return `comanda de ${label} é de outro terminal`;
      if (order.status !== "open") return `comanda de ${label} já está ${order.status}: item não muda de comanda fechada`;
    }
    return null;
  }

  /** Cadastro que ainda não existe aqui: a mudança vira o registro. */
  private async createFromUpdate(item: SyncItem, tx: Tx): Promise<ItemResult> {
    try {
      const serverSeq = await tx.savepoint((sp) => this.insertIdempotent(item, sp as never));
      if (serverSeq !== null) {
        return { client_uuid: item.client_uuid, status: "applied", server_seq: serverSeq };
      }
    } catch (error) {
      // Chave primária de outro restaurante, ou campo obrigatório ausente numa
      // mudança parcial. Os dois são recusa deste item, não do lote inteiro.
      const code = (error as { code?: string }).code;
      if (code !== "23505" && code !== "23502") throw error;
    }
    return reject(item, "o registro não pertence a este restaurante ou está incompleto");
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

function adapt(item: SyncItem): SyncItem {
  const adapter = ADAPTERS[item.entity_table];
  return adapter ? { ...item, payload: adapter(item.payload) } : { ...item, payload: { ...item.payload } };
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * Um UUID estável a partir de um texto — o mesmo item, reenviado, dá o mesmo
 * id, e o `ON CONFLICT` reconhece a repetição.
 */
function derivedUuid(seed: string): string {
  const hex = createHash("sha256").update(seed, "utf8").digest("hex");
  // Versão 8 (RFC 9562, "definida pela aplicação") e variante RFC.
  const variant = ((parseInt(hex[16]!, 16) & 0x3) | 0x8).toString(16);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-8${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
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
