/**
 * O contrato do push, com a fila de um caixa DE VERDADE.
 *
 * Até aqui cada lado era testado contra o próprio dublê: o caixa contra uma
 * nuvem falsa que aceitava qualquer payload, e a nuvem contra payloads escritos
 * à mão no formato dela. Os dois nunca se encontraram, e a primeira venda de
 * balcão com receita derrubava o lote inteiro com 500 (`quantity_mg` nulo) —
 * nada do caixa chegava aqui.
 *
 * Os arquivos em `contracts/` são gerados rodando os fluxos reais do caixa
 * (`apps/desktop-pdv/tests/push_day.py`). Este teste aplica cada um no
 * `SyncMerger` real, contra Postgres, com o papel da aplicação e o RLS ligado,
 * num lote só — do jeito que a rota de push faz.
 *
 * `push-day-1.1.2.json` é o que os caixas JÁ INSTALADOS têm na fila. Ele não
 * se regenera: é a garantia de que a nuvem continua aceitando o que já está
 * esperando nos balcões.
 */

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { SyncMerger, type ItemResult, type SyncItem } from "../src/lib/sync/merge.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

interface DayFile {
  caixa: string;
  device_secret_hex: string;
  items: SyncItem[];
}

function day(name: string): DayFile {
  const path = fileURLToPath(new URL(`../../../contracts/${name}`, import.meta.url));
  return JSON.parse(readFileSync(path, "utf8")) as DayFile;
}

const MOVEMENT_TABLES = [
  "audit_ledger", "order_item_ingredients", "order_items", "payments", "orders",
  "stock_movements", "cash_sessions", "cashback_ledger", "prepaid_ledger",
  "credit_account_ledger", "customer_credit_accounts", "customer_discount_tiers",
  "discount_tiers", "customers", "store_tables", "device_anchors", "fraud_alerts",
];

describeDb("contrato do push com a fila real do caixa", () => {
  let admin: postgres.Sql;
  let app: postgres.Sql;
  const tenants: string[] = [];

  beforeAll(() => {
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });
    app = postgres(APP_URL!, { max: 2, onnotice: () => {} });
  });

  afterAll(async () => {
    for (const tenant of tenants) {
      for (const table of MOVEMENT_TABLES) {
        await admin.unsafe(`DELETE FROM ${table} WHERE tenant_id = $1`, [tenant]);
      }
      await admin`DELETE FROM tenants WHERE id = ${tenant}`;
    }
    await admin?.end({ timeout: 5 });
    await app?.end({ timeout: 5 });
  });

  /** Um restaurante novo com um terminal que tem o segredo do arquivo. */
  async function terminal(file: DayFile) {
    const [t] = await admin<{ id: string }[]>`
      INSERT INTO tenants (name) VALUES (${"Contrato " + randomUUID()}) RETURNING id
    `;
    const tenant = t!.id;
    tenants.push(tenant);
    const [s] = await admin<{ id: string }[]>`
      INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Loja do contrato') RETURNING id
    `;
    const store = s!.id;
    const device = randomUUID();
    const secret = Buffer.from(file.device_secret_hex, "hex");
    await admin`
      INSERT INTO devices (id, tenant_id, store_id, token_hash)
      VALUES (${device}, ${tenant}, ${store}, ${randomUUID()})
    `;
    await admin`
      INSERT INTO device_secrets (tenant_id, device_id, secret)
      VALUES (${tenant}, ${device}, ${secret})
    `;
    const merger = new SyncMerger({ tenantId: tenant, storeId: store, deviceId: device, secret });
    /** Um lote, uma transação, o tenant declarado — como `withTenant`. */
    const push = (items: SyncItem[]) =>
      app.begin(async (tx) => {
        await tx`SELECT set_config('app.tenant_id', ${tenant}, true)`;
        return merger.apply(items, tx as never);
      }) as Promise<ItemResult[]>;
    return { tenant, store, push };
  }

  function refused(results: ItemResult[]) {
    return results.filter((r) => r.status === "rejected");
  }

  function count(tenant: string, table: string) {
    return admin.unsafe(`SELECT count(*)::int AS n FROM ${table} WHERE tenant_id = $1`, [tenant])
      .then((rows) => (rows[0] as unknown as { n: number }).n);
  }

  for (const name of ["push-day-1.1.2.json", "push-day.json"]) {
    describe(name, () => {
      const file = day(name);
      // Dividir e juntar conta entrou na 1.1.4: o dia congelado da 1.1.2 não
      // tem item mudando de comanda, e as contagens abaixo dependem disso.
      const splits = file.items.some(
        (i) => i.entity_table === "order_items" && i.operation === "update"
          && Object.hasOwn(i.payload, "order_id"),
      );
      let tenant: string;
      let push: (items: SyncItem[]) => Promise<ItemResult[]>;
      let results: ItemResult[];

      // Um restaurante por arquivo, o dia aplicado uma vez. Os ids do caixa são
      // UUIDs globais: aplicar o mesmo arquivo em dois tenants colidiria na
      // chave primária, coisa que dois caixas de verdade nunca fazem.
      beforeAll(async () => {
        ({ tenant, push } = await terminal(file));
        results = await push(file.items);
      });

      it("o dia inteiro entra num lote, sem recusa e sem 500", () => {
        expect(refused(results)).toEqual([]);
        expect(results).toHaveLength(file.items.length);
      });

      it("reenviar o dia inteiro não duplica nada", async () => {
        const before: Record<string, number> = {};
        for (const table of MOVEMENT_TABLES) before[table] = await count(tenant, table);

        const again = await push(file.items);

        expect(refused(again)).toEqual([]);
        for (const table of MOVEMENT_TABLES) {
          expect({ table, n: await count(tenant, table) }).toEqual({ table, n: before[table] });
        }
      });

      it("a baixa de estoque chega com a quantidade e o motivo", async () => {
        const moves = await admin<{ quantity_mg: string; reason: string; order_item_id: string | null }[]>`
          SELECT quantity_mg::text, reason, order_item_id FROM stock_movements
           WHERE tenant_id = ${tenant}
        `;
        const sent = file.items.filter((i) => i.entity_table === "stock_movements");
        expect(moves).toHaveLength(sent.length);
        const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);
        expect(sum(moves.map((m) => Number(m.quantity_mg)))).toBe(
          sum(sent.map((i) => Number(i.payload["qty_mg"] ?? i.payload["quantity_mg"]))),
        );
        // Venda sai com motivo "sale"; o estorno do cancelamento, "adjustment".
        expect(new Set(moves.map((m) => m.reason))).toEqual(new Set(["sale", "adjustment"]));
        expect(moves.every((m) => m.order_item_id)).toBe(true);
      });

      it("os insumos consumidos de cada item chegam — o CMV deixa de ser zero", async () => {
        const sent = file.items
          .filter((i) => i.entity_table === "order_items")
          .flatMap((i) => (i.payload["ingredients"] as { consumed_mg: number }[] | undefined) ?? []);
        const rows = await admin<{ consumed_mg: string }[]>`
          SELECT consumed_mg::text FROM order_item_ingredients WHERE tenant_id = ${tenant}
        `;
        expect(sent.length).toBeGreaterThan(0);
        expect(rows).toHaveLength(sent.length);
        expect(rows.reduce((s, r) => s + Number(r.consumed_mg), 0)).toBe(
          sent.reduce((s, r) => s + Number(r.consumed_mg), 0),
        );
      });

      it("a comanda da mesa chega ao estado final: paga, e a aberta por engano, cancelada", async () => {
        const waiter = await admin<{ status: string; tip_cents: string; bill: boolean }[]>`
          SELECT status, tip_cents::text, bill_requested_at IS NOT NULL AS bill
            FROM orders WHERE tenant_id = ${tenant} AND channel = 'waiter'
           ORDER BY status DESC
        `;
        // A mesa recebida, a parte paga, a comanda que recebeu a junção (pagas);
        // a aberta por engano e a que foi juntada (canceladas, zeradas).
        expect(waiter.map((o) => o.status)).toEqual(
          splits ? ["paid", "paid", "paid", "canceled", "canceled"] : ["paid", "canceled"],
        );
        const tipped = waiter.find((o) => o.tip_cents === "200");
        expect(tipped?.bill).toBe(true);
        // Toda comanda de mesa chega com a mesa: até a 1.1.3 o rótulo ia só como
        // `table_label`, e o painel mostrava mesa sem nome.
        const labels = await admin<{ customer_id: string | null }[]>`
          SELECT customer_id FROM orders WHERE tenant_id = ${tenant} AND channel = 'waiter'
        `;
        expect(labels.every((o) => (o.customer_id ?? "") !== "")).toBe(true);
      });

      it("dividir e juntar conta: cada item termina na comanda em que o caixa o deixou", async () => {
        const tabs = await admin<{ status: string; total_cents: string; live: string; live_total: string }[]>`
          SELECT o.status, o.total_cents::text,
                 count(i.id) FILTER (WHERE i.canceled_at IS NULL)::text AS live,
                 coalesce(sum(i.total_cents) FILTER (WHERE i.canceled_at IS NULL), 0)::text AS live_total
            FROM orders o
            LEFT JOIN order_items i ON i.order_id = o.id AND i.tenant_id = o.tenant_id
           WHERE o.tenant_id = ${tenant} AND o.channel = 'waiter'
           GROUP BY o.id, o.status, o.total_cents
        `;
        // Cada comanda paga soma os próprios itens vivos: o dinheiro não sumiu
        // nem apareceu na mudança de comanda.
        for (const tab of tabs.filter((t) => t.status === "paid")) {
          expect(tab.total_cents).toBe(tab.live_total);
        }
        // A parte paga (1 item), a mesa do começo (café + fatia) e a comanda
        // que recebeu tudo (1 dela + 1 movido + 1 da junção).
        expect(tabs.filter((t) => t.status === "paid").map((t) => Number(t.live)).sort())
          .toEqual(splits ? [1, 2, 3] : [1]);
        // Nenhuma cancelada ficou com item vivo: a juntada foi esvaziada antes,
        // e a aberta por engano teve os itens cancelados junto. A 1.1.2 não
        // enviava o cancelamento dos itens — defeito que a fila congelada guarda.
        expect(tabs.filter((t) => t.status === "canceled").every((t) => t.live === "0"))
          .toBe(splits);
      });

      it("mesa renomeada e mesa aposentada ficam como o caixa deixou", async () => {
        const tables = await admin<{ label: string; seats: number; is_active: boolean }[]>`
          SELECT label, seats, is_active FROM store_tables
           WHERE tenant_id = ${tenant} ORDER BY label
        `;
        expect(tables).toEqual([
          { label: "Mesa 99", seats: 4, is_active: false },
          { label: "Varanda 10", seats: 4, is_active: true },
        ]);
      });

      it("alterações de cadastro do caixa não se perdem por reusar o client_uuid", async () => {
        const [employee] = await admin<{ percent_basis_points: number }[]>`
          SELECT percent_basis_points FROM discount_tiers
           WHERE tenant_id = ${tenant} AND code = 'employee'
        `;
        expect(employee!.percent_basis_points).toBe(1500);
        const [limit] = await admin<{ limit_cents: string }[]>`
          SELECT limit_cents::text FROM customer_credit_accounts WHERE tenant_id = ${tenant}
        `;
        expect(limit!.limit_cents).toBe("30000");
        const [assigned] = await admin<{ code: string }[]>`
          SELECT t.code FROM customer_discount_tiers c
            JOIN discount_tiers t ON t.id = c.tier_id AND t.tenant_id = c.tenant_id
           WHERE c.tenant_id = ${tenant}
        `;
        expect(assigned!.code).toBe("gold");
      });

      if (name === "push-day.json") {
        it("a venda de balcão chega com número, operador e abertura", async () => {
          const counter = await admin<{ local_number: number | null; operator_id: string | null; opened_at: Date | null }[]>`
            SELECT local_number, operator_id, opened_at FROM orders
             WHERE tenant_id = ${tenant} AND channel = 'counter'
          `;
          expect(counter.length).toBe(3);
          for (const order of counter) {
            expect(order.local_number).toBeGreaterThan(0);
            expect(order.operator_id).toBeTruthy();
            expect(order.opened_at).toBeTruthy();
          }
        });

        it("o item cancelado chega cancelado — o upsell e o ranking o ignoram", async () => {
          const canceled = await admin<{ cancel_reason: string }[]>`
            SELECT cancel_reason FROM order_items
             WHERE tenant_id = ${tenant} AND canceled_at IS NOT NULL
          `;
          // A comanda aberta por engano tinha café e fatia: os dois saem cancelados.
          expect(canceled.map((i) => i.cancel_reason).sort()).toEqual([
            "Cliente desistiu",
            "[comanda cancelada] Mesa aberta por engano",
            "[comanda cancelada] Mesa aberta por engano",
          ]);
        });

        it("o item lançado pelo garçom chega com nome e preço", async () => {
          const items = await admin<{ product_name: string; unit_price_cents: string }[]>`
            SELECT i.product_name, i.unit_price_cents::text FROM order_items i
              JOIN orders o ON o.id = i.order_id AND o.tenant_id = i.tenant_id
             WHERE i.tenant_id = ${tenant} AND o.channel = 'waiter'
          `;
          // Mesa do começo (2), aberta por engano (2), e as da divisão (3 + 1).
          expect(items.length).toBe(splits ? 8 : 2);
          for (const item of items) {
            expect(item.product_name).not.toBe("");
            expect(Number(item.unit_price_cents)).toBeGreaterThan(0);
          }
        });

        it("o insumo do item de mesa chega, e a comanda cancelada o devolve", async () => {
          // Até a 1.1.5 o garçom lançava sem baixar insumo: a mesa não aparecia
          // no estoque nem no CMV do painel, que só enxergava o balcão.
          const byOrder = await admin<{ status: string; ingredients: string; net_mg: string; sales: string; reversals: string }[]>`
            SELECT o.status,
                   (SELECT count(*) FROM order_item_ingredients g
                     JOIN order_items gi ON gi.id = g.order_item_id AND gi.tenant_id = g.tenant_id
                    WHERE gi.order_id = o.id AND g.tenant_id = o.tenant_id)::text AS ingredients,
                   coalesce(sum(m.quantity_mg), 0)::text AS net_mg,
                   count(m.id) FILTER (WHERE m.reason = 'sale')::text AS sales,
                   count(m.id) FILTER (WHERE m.reason = 'adjustment')::text AS reversals
              FROM orders o
              JOIN order_items i ON i.order_id = o.id AND i.tenant_id = o.tenant_id
              LEFT JOIN stock_movements m ON m.order_item_id = i.id::text AND m.tenant_id = i.tenant_id
             WHERE o.tenant_id = ${tenant} AND o.channel = 'waiter'
             GROUP BY o.id, o.status
            HAVING count(m.id) > 0
          `;
          // A mesa recebida baixou a fatia; a aberta por engano baixou e estornou.
          expect(byOrder.map((o) => o.status).sort()).toEqual(["canceled", "paid"]);
          for (const order of byOrder) expect(Number(order.ingredients)).toBe(5);
          const paid = byOrder.find((o) => o.status === "paid")!;
          expect([Number(paid.sales), Number(paid.reversals)]).toEqual([5, 0]);
          expect(Number(paid.net_mg)).toBeLessThan(0);
          const canceled = byOrder.find((o) => o.status === "canceled")!;
          expect([Number(canceled.sales), Number(canceled.reversals)]).toEqual([5, 5]);
          expect(Number(canceled.net_mg)).toBe(0);
        });

        it("uma atualização não regride comanda já paga", async () => {
          const paid = file.items.find(
            (i) => i.entity_table === "orders" && i.operation === "update" && i.payload["status"] === "paid",
          )!;

          const [result] = await push([{
            ...paid,
            client_uuid: randomUUID(),
            payload: { id: paid.payload["id"], status: "canceled" },
          }]);

          expect(result!.status).toBe("rejected");
          const [row] = await admin<{ status: string }[]>`
            SELECT status FROM orders WHERE tenant_id = ${tenant} AND id = ${String(paid.payload["id"])}
          `;
          expect(row!.status).toBe("paid");
        });

        it("uma mudança antiga que chega depois não desfaz a recente", async () => {
          const renamed = file.items.find(
            (i) => i.entity_table === "store_tables" && i.operation === "update" && i.payload["label"],
          )!;
          const older = new Date(Date.parse(String(renamed.payload["updated_at"])) - 60_000);

          const [result] = await push([{
            ...renamed,
            client_uuid: randomUUID(),
            payload: { ...renamed.payload, label: "Rótulo de ontem", updated_at: older.toISOString() },
          }]);

          expect(result!.status).toBe("duplicate");
          const labels = await admin<{ label: string }[]>`
            SELECT label FROM store_tables WHERE tenant_id = ${tenant}
          `;
          expect(labels.map((l) => l.label)).toContain(String(renamed.payload["label"]));
          expect(labels.map((l) => l.label)).not.toContain("Rótulo de ontem");
        });

        it("cadastro cuja criação se perdeu nasce da própria mudança", async () => {
          const changed = file.items.find(
            (i) => i.entity_table === "discount_tiers" && i.operation === "update",
          )!;
          const id = randomUUID();

          const [result] = await push([{
            ...changed,
            entity_id: id,
            client_uuid: randomUUID(),
            payload: { ...changed.payload, id, code: "diamond", name: "Diamante" },
          }]);

          expect(result!.status).toBe("applied");
          const [row] = await admin<{ percent_basis_points: number }[]>`
            SELECT percent_basis_points FROM discount_tiers WHERE tenant_id = ${tenant} AND id = ${id}
          `;
          expect(row!.percent_basis_points).toBe(Number(changed.payload["percent_basis_points"]));
        });

        it("movimento cuja criação se perdeu é recusado, nunca inventado", async () => {
          const [result] = await push([{
            entity_table: "orders",
            entity_id: randomUUID(),
            client_uuid: randomUUID(),
            operation: "update",
            payload: { id: randomUUID(), status: "paid", total_cents: 5_000 },
          }]);

          // Um "pago" sem a venda seria faturamento sem venda.
          expect(result!.status).toBe("rejected");
        });

        it("uma atualização com id de outro restaurante é recusada, e lá nada muda", async () => {
          const other = await terminal(file);
          const table = file.items.find((i) => i.entity_table === "store_tables" && i.operation === "update")!;
          const order = file.items.find((i) => i.entity_table === "orders" && i.operation === "update")!;

          const answers = await other.push([
            { ...table, client_uuid: randomUUID(), payload: { ...table.payload, label: "Invadida" } },
            { ...order, client_uuid: randomUUID(), payload: { id: order.payload["id"], tip_cents: 99_999 } },
          ]);

          // Virar cadastro novo aqui deixaria um terminal clonado descobrir, por
          // tentativa, quais ids existem no outro restaurante.
          expect(answers.map((a) => a.status)).toEqual(["rejected", "rejected"]);
          const labels = await admin<{ label: string }[]>`
            SELECT label FROM store_tables WHERE tenant_id = ${tenant}
          `;
          expect(labels.map((l) => l.label)).not.toContain("Invadida");
          const tips = await admin<{ tip_cents: string }[]>`
            SELECT tip_cents::text FROM orders WHERE tenant_id = ${tenant}
          `;
          expect(tips.map((t) => t.tip_cents)).not.toContain("99999");
        });
      }
    });
  }
});
