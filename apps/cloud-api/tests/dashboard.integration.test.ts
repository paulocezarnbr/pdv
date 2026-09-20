import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

describeDb("painel contra PostgreSQL real", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let otherTenant: string;
  let store: string;
  let sessionToken: string;
  let GET: (request: Request) => Promise<Response>;

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "dashboard-integration-secret-with-32-chars";
    ({ GET } = await import("../src/app/api/panel/dashboard/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Painel Integração') RETURNING id`;
    const [other] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Outro restaurante') RETURNING id`;
    tenant = t!.id; otherTenant = other!.id;
    const [s] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Loja Centro') RETURNING id`;
    const [otherStore] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${otherTenant}, 'Loja alheia') RETURNING id`;
    store = s!.id;

    const user = randomUUID();
    await admin`INSERT INTO panel_users (id, tenant_id, email, name, role, password_hash, can_authorize) VALUES (${user}, ${tenant}, 'dono@teste.local', 'Dono Teste', 'owner', 'hash-inutil', true)`;
    sessionToken = "sessao-dashboard-integracao";
    await admin`INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at) VALUES (${createHash("sha256").update(sessionToken).digest("hex")}, ${tenant}, ${user}, now() + interval '1 hour')`;

    const staff = randomUUID();
    await admin`INSERT INTO users (id, tenant_id, name, login, role, pin_hash) VALUES (${staff}, ${tenant}, 'João Garçom', 'joao-teste', 'waiter', 'argon2')`;
    const device = randomUUID();
    await admin`INSERT INTO devices (id, tenant_id, store_id, label, token_hash, last_seen_at) VALUES (${device}, ${tenant}, ${store}, 'Caixa 1', ${randomUUID()}, now())`;
    const order = randomUUID();
    await admin`INSERT INTO orders (id, tenant_id, store_id, device_id, client_uuid, local_number, status, served_by_user_id, subtotal_cents, discount_cents, total_cents, tip_cents, opened_at, closed_at) VALUES (${order}, ${tenant}, ${store}, ${device}, ${randomUUID()}, 1, 'paid', ${staff}, 4200, 200, 4000, 500, now() - interval '30 minutes', now() - interval '10 minutes')`;
    const item = randomUUID();
    await admin`INSERT INTO order_items (id, tenant_id, order_id, client_uuid, product_name, total_cents, created_at) VALUES (${item}, ${tenant}, ${order}, ${randomUUID()}, 'Torta de chocolate', 4000, now() - interval '20 minutes')`;
    await admin`INSERT INTO order_item_ingredients (id, tenant_id, order_item_id, client_uuid, inventory_item_id, inventory_item_name, consumed_mg, unit_cost_cents) VALUES (${randomUUID()}, ${tenant}, ${item}, ${randomUUID()}, 'chocolate', 'Chocolate', 100000, 900)`;
    await admin`INSERT INTO fraud_alerts (tenant_id, store_id, device_id, reason) VALUES (${tenant}, ${store}, ${device}, 'Tentativa de reescrita')`;

    await admin`INSERT INTO orders (id, tenant_id, store_id, device_id, client_uuid, local_number, status, total_cents, opened_at, closed_at) VALUES (${randomUUID()}, ${otherTenant}, ${otherStore!.id}, ${randomUUID()}, ${randomUUID()}, 99, 'paid', 999999, now() - interval '20 minutes', now() - interval '5 minutes')`;
  });

  afterAll(async () => {
    if (!admin) return;
    for (const id of [tenant, otherTenant]) {
      await admin`DELETE FROM order_item_ingredients WHERE tenant_id = ${id}`;
      await admin`DELETE FROM order_items WHERE tenant_id = ${id}`;
      await admin`DELETE FROM orders WHERE tenant_id = ${id}`;
      await admin`DELETE FROM fraud_alerts WHERE tenant_id = ${id}`;
      await admin`DELETE FROM users WHERE tenant_id = ${id}`;
      await admin`DELETE FROM tenants WHERE id = ${id}`;
    }
    await admin.end({ timeout: 5 });
  });

  it("exige sessão administrativa", async () => {
    const response = await GET(new Request("http://localhost/api/panel/dashboard"));
    expect(response.status).toBe(401);
  });

  it("calcula o período e não mistura o faturamento do outro tenant", async () => {
    const from = new Date(Date.now() - 86_400_000).toISOString();
    const to = new Date(Date.now() + 60_000).toISOString();
    const response = await GET(new Request(
      `http://localhost/api/panel/dashboard?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,
      { headers: { cookie: `erp_session=${sessionToken}` } },
    ));
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(Number(body.summary.revenue_cents)).toBe(4000);
    expect(Number(body.summary.tips_cents)).toBe(500);
    expect(Number(body.summary.cmv_cents)).toBe(900);
    expect(body.products[0].name).toBe("Torta de chocolate");
    expect(body.staff[0].name).toBe("João Garçom");
    expect(body.devices[0].label).toBe("Caixa 1");
    expect(body.alerts[0].reason).toBe("Tentativa de reescrita");
  });

  it("recusa loja de outro tenant", async () => {
    const response = await GET(new Request(
      `http://localhost/api/panel/dashboard?store=${randomUUID()}`,
      { headers: { cookie: `erp_session=${sessionToken}` } },
    ));
    expect(response.status).toBe(404);
  });
});
