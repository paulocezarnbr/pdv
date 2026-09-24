/**
 * `update` vindo do terminal, contra PostgreSQL real e pelo papel `erp_app`.
 *
 * Até aqui a nuvem só sabia inserir. O `update` de pedido virava INSERT com o
 * mesmo `id`, batia no NOT NULL de `local_number`, e o lote caía com ele —
 * pedir a conta, trocar a mesa e receber a comanda nunca chegavam. Estes
 * testes cobrem o que o UPDATE de verdade precisa garantir: aplica uma vez,
 * só nas colunas que mudam na vida da entidade, e só na linha que aquele
 * terminal pode alterar.
 */

import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { SyncMerger, type SyncItem } from "../src/lib/sync/merge.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

describeDb("update do terminal na nuvem", () => {
  let admin: postgres.Sql;
  let app: postgres.Sql;
  let tenant: string;
  let store: string;
  const device = randomUUID();
  const otherDevice = randomUUID();

  beforeAll(async () => {
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });
    app = postgres(APP_URL!, { max: 1, onnotice: () => {} });
    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Sync update') RETURNING id`;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Centro') RETURNING id`;
    store = s!.id;
  });

  afterAll(async () => {
    if (!admin) return;
    await admin`DELETE FROM sync_applied_updates WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM order_items WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM orders WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM discount_tiers WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM tenants WHERE id=${tenant}`;
    await app.end({ timeout: 5 });
    await admin.end({ timeout: 5 });
  });

  /** Um lote pelo papel da aplicação, com o tenant declarado — como a rota. */
  async function push(items: SyncItem[], from = device) {
    const merger = new SyncMerger({
      tenantId: tenant, storeId: store, deviceId: from, secret: Buffer.from("x"),
    });
    return app.begin(async (tx) => {
      await tx`SELECT set_config('app.tenant_id', ${tenant}, true)`;
      return merger.apply(items, tx);
    });
  }

  function openOrder(id = randomUUID()): SyncItem {
    return {
      entity_table: "orders", entity_id: id, client_uuid: randomUUID(), operation: "insert",
      payload: {
        id, local_number: 7, channel: "waiter", status: "open", operator_id: "ana",
        opened_at: "2026-09-23T20:00:00Z", table_id: "mesa-4", customer_id: "Mesa 4",
      },
    };
  }

  function item(orderId: string, id = randomUUID()): SyncItem {
    return {
      entity_table: "order_items", entity_id: id, client_uuid: randomUUID(), operation: "insert",
      payload: { id, order_id: orderId, product_name: "Café", unit_price_cents: 700, total_cents: 700 },
    };
  }

  function update(table: string, id: string, payload: Record<string, unknown>): SyncItem {
    return {
      entity_table: table, entity_id: id, client_uuid: randomUUID(), operation: "update",
      payload: { id, ...payload },
    };
  }

  async function order(id: string) {
    const [row] = await admin<{ status: string; total_cents: string; customer_id: string;
      bill_requested_at: Date | null; local_number: number }[]>`
      SELECT status, total_cents, customer_id, bill_requested_at, local_number
        FROM orders WHERE id=${id}`;
    return row!;
  }

  it("o ciclo da mesa chega inteiro: conta pedida, mesa trocada, recebida", async () => {
    const open = openOrder();
    const id = open.entity_id;
    const results = await push([
      open,
      update("orders", id, { bill_requested_at: "2026-09-23T21:00:00Z" }),
      update("orders", id, { table_id: "mesa-9", customer_id: "Mesa 9" }),
      update("orders", id, { status: "paid", total_cents: 2100, tip_cents: 210,
                             closed_at: "2026-09-23T21:10:00Z" }),
    ]);

    expect(results.map((r) => r.status)).toEqual(["applied", "applied", "applied", "applied"]);
    const row = await order(id);
    expect(row.status).toBe("paid");
    expect(row.customer_id).toBe("Mesa 9");
    expect(Number(row.total_cents)).toBe(2100);
  });

  it("reenviar a mesma atualização não a aplica de novo", async () => {
    // O lote reenviado depois de uma resposta perdida. Se a troca de mesa
    // fosse reaplicada depois de uma troca mais nova, a mesa voltaria atrás.
    const open = openOrder();
    const id = open.entity_id;
    const toNine = update("orders", id, { customer_id: "Mesa 9" });
    const toTwo = update("orders", id, { customer_id: "Mesa 2" });
    await push([open, toNine, toTwo]);

    const [replayed] = await push([toNine]);

    expect(replayed!.status).toBe("duplicate");
    expect((await order(id)).customer_id).toBe("Mesa 2");
  });

  it("coluna que não muda na vida do pedido é ignorada", async () => {
    const open = openOrder();
    await push([open, update("orders", open.entity_id, { local_number: 999, bill_requested_at: null })]);

    expect((await order(open.entity_id)).local_number).toBe(7);
  });

  it("pedido fechado não se edita — corrige-se por estorno", async () => {
    const open = openOrder();
    const id = open.entity_id;
    await push([open, update("orders", id, { status: "paid", total_cents: 2100 })]);

    const [late] = await push([update("orders", id, { total_cents: 100 })]);

    expect(late!.status).toBe("rejected");
    expect(late!.message).toContain("já está paid");
    expect(Number((await order(id)).total_cents)).toBe(2100);
  });

  it("um terminal não altera o pedido de outro", async () => {
    const open = openOrder();
    await push([open]);

    const [foreign] = await push([update("orders", open.entity_id, { status: "canceled" })], otherDevice);

    expect(foreign!.status).toBe("rejected");
    expect(foreign!.message).toContain("outro terminal");
    expect((await order(open.entity_id)).status).toBe("open");
  });

  it("item só muda para comanda aberta do mesmo terminal", async () => {
    const mine = openOrder();
    const theirs = openOrder();
    const cafe = item(mine.entity_id);
    await push([mine, cafe]);
    await push([theirs], otherDevice);

    const [moved] = await push([update("order_items", cafe.entity_id, { order_id: theirs.entity_id })]);

    expect(moved!.status).toBe("rejected");
    expect(moved!.message).toContain("pedido de destino");
  });

  it("atualização que chega antes da linha é recusada, não derruba o lote", async () => {
    const orphan = update("orders", randomUUID(), { bill_requested_at: "2026-09-23T21:00:00Z" });
    const fine = openOrder();

    const results = await push([orphan, fine]);

    expect(results.map((r) => r.status)).toEqual(["rejected", "applied"]);
    expect(results[0]!.message).toContain("ainda não chegou");
  });

  it("linha que o banco recusa vira quarentena sozinha, e o resto do lote entra", async () => {
    // Era o defeito inteiro: um pedido sem `local_number` derrubava o lote, e
    // a fila do terminal nunca mais andava.
    const broken: SyncItem = {
      entity_table: "orders", entity_id: randomUUID(), client_uuid: randomUUID(),
      operation: "insert", payload: { status: "paid", total_cents: 900 },
    };
    const fine = openOrder();

    const results = await push([broken, fine]);

    expect(results[0]!.status).toBe("rejected");
    expect(results[0]!.message).toContain("23502");
    expect(results[1]!.status).toBe("applied");
    expect((await order(fine.entity_id)).status).toBe("open");
  });

  it("cadastro da loja: o nível de desconto atualiza, de outra loja não", async () => {
    const id = randomUUID();
    const [created] = await push([{
      entity_table: "discount_tiers", entity_id: id, client_uuid: randomUUID(), operation: "insert",
      payload: { id, code: "ouro", name: "Ouro", percent_basis_points: 1000, priority: 3,
                 updated_at: "2026-09-23T20:00:00Z" },
    }]);
    expect(created!.status).toBe("applied");

    const [applied] = await push([update("discount_tiers", id, { percent_basis_points: 1200 })]);
    expect(applied!.status).toBe("applied");
    const [row] = await admin<{ percent_basis_points: number }[]>`
      SELECT percent_basis_points FROM discount_tiers WHERE id=${id}`;
    expect(row!.percent_basis_points).toBe(1200);

    const [otherStore] = await admin<{ id: string }[]>`
      INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Outra') RETURNING id`;
    const merger = new SyncMerger({
      tenantId: tenant, storeId: otherStore!.id, deviceId: device, secret: Buffer.from("x"),
    });
    const [refused] = await app.begin(async (tx) => {
      await tx`SELECT set_config('app.tenant_id', ${tenant}, true)`;
      return merger.apply([update("discount_tiers", id, { percent_basis_points: 5000 })], tx);
    });
    expect(refused!.status).toBe("rejected");
    await admin`DELETE FROM stores WHERE id=${otherStore!.id}`;
  });

  it("o registro de idempotência não pode ser apagado pela aplicação", async () => {
    await expect(app`DELETE FROM sync_applied_updates WHERE tenant_id=${tenant}`).rejects.toThrow();
  });
});
