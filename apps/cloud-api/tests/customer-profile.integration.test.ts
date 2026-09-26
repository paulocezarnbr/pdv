/**
 * O cadastro do cliente vindo do caixa (schema 15, migração 020): morador e
 * apartamento, contato, CPF e consentimento — e a mesma pessoa cadastrada em
 * dois caixas, que não pode travar a sincronização.
 */

import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { SyncMerger, type ItemResult, type SyncItem } from "../src/lib/sync/merge.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

describeDb("cadastro do cliente pelo caixa", () => {
  let admin: postgres.Sql;
  let app: postgres.Sql;
  let tenant: string;
  let store: string;

  beforeAll(async () => {
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });
    app = postgres(APP_URL!, { max: 2, onnotice: () => {} });
    const [t] = await admin<{ id: string }[]>`
      INSERT INTO tenants (name) VALUES (${"Condomínio " + randomUUID()}) RETURNING id
    `;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`
      INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Mercadinho do condomínio') RETURNING id
    `;
    store = s!.id;
  });

  afterAll(async () => {
    await admin`DELETE FROM customers WHERE tenant_id = ${tenant}`;
    await admin`DELETE FROM devices WHERE tenant_id = ${tenant}`;
    await admin`DELETE FROM stores WHERE tenant_id = ${tenant}`;
    await admin`DELETE FROM tenants WHERE id = ${tenant}`;
    await admin?.end({ timeout: 5 });
    await app?.end({ timeout: 5 });
  });

  /** Um caixa da loja: cada um tem o próprio banco e cadastra os próprios clientes. */
  async function terminal() {
    const device = randomUUID();
    await admin`
      INSERT INTO devices (id, tenant_id, store_id, token_hash) VALUES (${device}, ${tenant}, ${store}, ${randomUUID()})
    `;
    const merger = new SyncMerger({ tenantId: tenant, storeId: store, deviceId: device, secret: Buffer.alloc(32) });
    return (items: SyncItem[]) =>
      app.begin(async (tx) => {
        await tx`SELECT set_config('app.tenant_id', ${tenant}, true)`;
        return merger.apply(items, tx as never);
      }) as Promise<ItemResult[]>;
  }

  function customer(fields: Record<string, unknown>, operation: "insert" | "update" = "insert"): SyncItem {
    const id = (fields["id"] as string | undefined) ?? randomUUID();
    return {
      entity_table: "customers",
      entity_id: id,
      client_uuid: randomUUID(),
      operation,
      payload: { id, ...fields },
    };
  }

  const lia = {
    name: "Lia Cliente", phone: "21998765432", email: "lia@exemplo.com", cpf: "52998224725",
    is_resident: true, unit_block: "B", unit_number: "101", birth_date: "1990-09-26",
    marketing_opt_in: true, marketing_opt_in_at: "2026-09-26T15:00:00.000+00:00",
    created_at: "2026-09-26T15:00:00.000+00:00", updated_at: "2026-09-26T15:00:00.000+00:00",
  };

  async function row(id: string) {
    const [found] = await admin<Record<string, unknown>[]>`
      SELECT name, phone, email, cpf, is_resident, unit_block, unit_number, birth_date::text AS birth_date,
             marketing_opt_in, marketing_opt_in_at, created_at
        FROM customers WHERE tenant_id = ${tenant} AND id::text = ${id}
    `;
    return found;
  }

  it("o cadastro completo chega com morador, apartamento e consentimento", async () => {
    const push = await terminal();
    const item = customer({ ...lia, phone: "21900000001", cpf: "11144477735" });

    const [result] = await push([item]);

    expect(result!.status).toBe("applied");
    expect(await row(item.entity_id)).toMatchObject({
      name: "Lia Cliente", phone: "21900000001", email: "lia@exemplo.com", cpf: "11144477735",
      is_resident: true, unit_block: "B", unit_number: "101", birth_date: "1990-09-26", marketing_opt_in: true,
    });
    expect((await row(item.entity_id))!["marketing_opt_in_at"]).toEqual(new Date("2026-09-26T15:00:00Z"));
  });

  it("a correção mais nova vence, a atrasada não desfaz, e a criação não muda", async () => {
    const push = await terminal();
    const created = customer({ ...lia, phone: "21900000002", cpf: null });
    await push([created]);

    const edit = customer({
      id: created.entity_id, name: "Lia Souza", phone: "21900000002", unit_number: "302", is_resident: true,
      marketing_opt_in: false, marketing_opt_in_at: null, updated_at: "2026-09-29T15:00:00.000+00:00",
    }, "update");
    const late = customer({
      id: created.entity_id, name: "Lia Antiga", updated_at: "2026-09-27T15:00:00.000+00:00",
    }, "update");

    const results = await push([edit, late]);

    expect(results.map((r) => r.status)).toEqual(["applied", "duplicate"]);
    expect(await row(created.entity_id)).toMatchObject({
      name: "Lia Souza", unit_number: "302", marketing_opt_in: false, marketing_opt_in_at: null,
      created_at: new Date("2026-09-26T15:00:00Z"),
    });
  });

  it("a mesma pessoa em dois caixas: o segundo WhatsApp é recusado sozinho, sem travar o lote", async () => {
    const first = await terminal();
    const second = await terminal();
    await first([customer({ ...lia, phone: "21900000003", cpf: "52998224725" })]);

    const repeated = customer({ ...lia, phone: "21900000003", cpf: "52998224725" });
    const other = customer({ ...lia, name: "Rui Vizinho", phone: "21900000004", cpf: null });
    const results = await second([repeated, other]);

    expect(results[0]).toMatchObject({ status: "rejected" });
    expect(results[0]!.message).toContain("WhatsApp");
    expect(results[1]!.status).toBe("applied");
    expect(await row(other.entity_id)).toMatchObject({ name: "Rui Vizinho" });
  });

  it("o mesmo CPF em dois caixas entra nos dois: juntar cadastros é do painel", async () => {
    const first = await terminal();
    const second = await terminal();
    const a = customer({ ...lia, phone: "21900000005", cpf: "39053344705" });
    const b = customer({ ...lia, phone: "21900000006", cpf: "39053344705" });

    expect((await first([a]))[0]!.status).toBe("applied");
    expect((await second([b]))[0]!.status).toBe("applied");
  });

  it("a correção de um cliente cuja criação se perdeu cria o cadastro", async () => {
    const push = await terminal();
    const orphan = customer({ ...lia, phone: "21900000007", cpf: null }, "update");

    const [result] = await push([orphan]);

    expect(result!.status).toBe("applied");
    expect(await row(orphan.entity_id)).toMatchObject({ phone: "21900000007", is_resident: true });
  });

  it("o caixa antigo, sem as colunas novas, continua entrando com os padrões", async () => {
    const push = await terminal();
    const old = customer({ name: "Cliente Antigo", phone: "21900000008", created_at: lia.created_at, updated_at: lia.updated_at });

    await push([old]);

    expect(await row(old.entity_id)).toMatchObject({
      is_resident: false, marketing_opt_in: false, email: null, cpf: null, unit_number: null,
    });
  });
});
