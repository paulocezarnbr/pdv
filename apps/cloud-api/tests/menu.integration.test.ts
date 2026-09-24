/**
 * O cardápio QR contra PostgreSQL real, com o papel `erp_app` (sem BYPASSRLS).
 *
 * O cardápio é a única leitura da retaguarda sem sessão. Estes testes guardam o
 * que não pode escapar dela: outro restaurante, produto oculto, venda
 * cancelada, link revogado e restaurante suspenso.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

type Handler = (request: Request) => Promise<Response>;

describeDb("cardápio QR contra PostgreSQL real", () => {
  let admin: postgres.Sql;
  let loadMenu: (token: string) => Promise<import("../src/lib/menu/load.ts").MenuData | null>;
  let clearMenuCache: () => void;
  let GET: Handler; let POST: Handler; let DELETE: Handler; let PUT_PRODUCT: Handler; let QR: Handler;

  let tenant: string; let other: string; let store: string; let otherStore: string;
  let owner: string; let ownerSession: string; let viewerSession: string; let otherSession: string;
  const ids: Record<string, string> = {};
  const id = (sku: string): string => ids[sku]!;

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "menu-integration-secret-with-32-chars-ok";
    process.env.PUBLIC_BASE_URL = "https://painel.exemplo.com.br";
    ({ loadMenu, clearMenuCache } = await import("../src/lib/menu/load.ts"));
    ({ GET, POST, DELETE } = await import("../src/app/api/panel/menu/route.ts"));
    ({ PUT: PUT_PRODUCT } = await import("../src/app/api/panel/menu/products/route.ts"));
    ({ GET: QR } = await import("../src/app/api/panel/menu/qr/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const first = (rows: { id: string }[]): string => rows[0]!.id;
    tenant = first(await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Café Central') RETURNING id`);
    other = first(await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Concorrente') RETURNING id`);
    store = first(await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Loja Centro') RETURNING id`);
    otherStore = first(await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${other}, 'Loja Rival') RETURNING id`);

    owner = randomUUID();
    ownerSession = await session(tenant, owner, "owner");
    viewerSession = await session(tenant, randomUUID(), "viewer");
    otherSession = await session(other, randomUUID(), "owner");

    const product = async (tenantId: string, sku: string, name: string, category: string | null, price: number, extra: { visible?: boolean; active?: boolean } = {}) => {
      const [row] = await admin<{ id: string }[]>`
        INSERT INTO products (tenant_id, sku, name, category, price_cents, menu_visible, is_active)
        VALUES (${tenantId}, ${sku}, ${name}, ${category}, ${price}, ${extra.visible ?? true}, ${extra.active ?? true})
        RETURNING id::text`;
      ids[sku] = row!.id;
    };
    await product(tenant, "CAFE", "Café coado", "Bebidas", 700);
    await product(tenant, "PAO", "Pão de queijo", "Salgados", 650);
    await product(tenant, "BOLO", "Bolo de cenoura", "Doces", 1200);
    await product(tenant, "SUCO", "Suco de laranja", "Bebidas", 1100);
    await product(tenant, "SEGREDO", "Prato da equipe", "Salgados", 100, { visible: false });
    await product(tenant, "VELHO", "Produto descontinuado", null, 100, { active: false });
    await product(other, "RIVAL", "Café do concorrente", "Bebidas", 500);

    // Vendas pagas: café + pão em 5 pedidos, café + bolo em 3, café sozinho em 2.
    const sale = async (items: string[], extra: { status?: string; canceled?: string[]; tenantId?: string; storeId?: string } = {}) => {
      const order = randomUUID();
      const tenantId = extra.tenantId ?? tenant;
      await admin`
        INSERT INTO orders (id, tenant_id, store_id, device_id, client_uuid, local_number, status, total_cents, opened_at, closed_at)
        VALUES (${order}, ${tenantId}, ${extra.storeId ?? store}, ${randomUUID()}, ${randomUUID()}, 1,
                ${extra.status ?? "paid"}, 1000, now() - interval '2 days', now() - interval '2 days')`;
      for (const sku of items) {
        await admin`
          INSERT INTO order_items (id, tenant_id, order_id, client_uuid, product_id, product_name, total_cents, created_at, canceled_at)
          VALUES (${randomUUID()}, ${tenantId}, ${order}, ${randomUUID()}, ${id(sku)}, ${sku}, 100, now(),
                  ${extra.canceled?.includes(sku) ? new Date() : null})`;
      }
    };
    for (let i = 0; i < 5; i++) await sale(["CAFE", "PAO"]);
    for (let i = 0; i < 3; i++) await sale(["CAFE", "BOLO"]);
    for (let i = 0; i < 2; i++) await sale(["CAFE"]);
    // Nada disto pode contar: aberto, cancelado, item oculto, e outra rede.
    for (let i = 0; i < 6; i++) await sale(["CAFE", "SUCO"], { status: "open" });
    for (let i = 0; i < 6; i++) await sale(["CAFE", "SUCO"], { canceled: ["SUCO"] });
    for (let i = 0; i < 6; i++) await sale(["CAFE", "SEGREDO"]);
    for (let i = 0; i < 6; i++) await sale(["RIVAL"], { tenantId: other, storeId: otherStore });
  });

  beforeEach(() => clearMenuCache());

  afterAll(async () => {
    if (!admin) return;
    await admin`SET session_replication_role = replica`;
    try {
      for (const id of [tenant, other]) {
        await admin`DELETE FROM panel_admin_events WHERE tenant_id = ${id}`;
        await admin`DELETE FROM menu_links WHERE tenant_id = ${id}`;
        await admin`DELETE FROM order_items WHERE tenant_id = ${id}`;
        await admin`DELETE FROM orders WHERE tenant_id = ${id}`;
        await admin`DELETE FROM tenants WHERE id = ${id}`;
      }
    } finally {
      await admin`SET session_replication_role = origin`;
    }
    await admin.end({ timeout: 5 });
  });

  async function session(tenantId: string, userId: string, role: string): Promise<string> {
    await admin`
      INSERT INTO panel_users (id, tenant_id, email, name, role, password_hash, can_authorize)
      VALUES (${userId}, ${tenantId}, ${`${role}-${userId}@teste.local`}, ${role}, ${role}, 'hash', true)`;
    const token = `sessao-${randomUUID()}`;
    await admin`
      INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at)
      VALUES (${createHash("sha256").update(token).digest("hex")}, ${tenantId}, ${userId}, now() + interval '1 hour')`;
    return token;
  }

  function call(handler: Handler, method: string, cookie: string, body?: unknown, query = ""): Promise<Response> {
    return handler(new Request(`http://localhost/api/panel/menu${query}`, {
      method,
      headers: { cookie: `erp_session=${cookie}`, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }));
  }

  async function createLink(tableLabel?: string): Promise<{ id: string; url: string; token: string }> {
    const response = await call(POST, "POST", ownerSession, { store_id: store, table_label: tableLabel });
    expect(response.status).toBe(201);
    const body = await response.json();
    return { ...body, token: body.url.split("/cardapio/")[1] };
  }

  it("o QR abre o cardápio da loja, por categoria e com o preço do cadastro", async () => {
    const link = await createLink("Mesa 5");
    expect(link.url).toMatch(/^https:\/\/painel\.exemplo\.com\.br\/cardapio\/[A-Za-z0-9_-]{24}$/);

    const menu = await loadMenu(link.token);

    expect(menu?.storeName).toBe("Loja Centro");
    expect(menu?.tableLabel).toBe("Mesa 5");
    expect(menu?.categories.map((c) => c.name)).toEqual(["Bebidas", "Doces", "Salgados"]);
    const bebidas = menu!.categories[0]!.items.map((i) => [i.name, i.priceCents]);
    expect(bebidas).toEqual([["Café coado", 700], ["Suco de laranja", 1100]]);
  });

  it("nunca mostra produto oculto, inativo ou de outro restaurante", async () => {
    const menu = await loadMenu((await createLink()).token);
    const names = menu!.categories.flatMap((c) => c.items.map((i) => i.name));

    expect(names).not.toContain("Prato da equipe");
    expect(names).not.toContain("Produto descontinuado");
    expect(names).not.toContain("Café do concorrente");
  });

  it("sugere o que vende junto, só com venda paga e item não cancelado", async () => {
    const menu = await loadMenu((await createLink()).token);
    const withCoffee = menu!.companions[id("CAFE")]!.map((c) => c.productId);

    // 5 de 22 pedidos pagos com café têm pão; 3 têm bolo. O suco só aparece
    // em pedido aberto ou cancelado, e o prato oculto não pode ser sugerido.
    expect(withCoffee).toEqual([id("PAO"), id("BOLO")]);
    expect(withCoffee).not.toContain(id("SUCO"));
    expect(withCoffee).not.toContain(id("SEGREDO"));
    expect(menu!.companions[id("PAO")]!.map((c) => c.productId)).toEqual([id("CAFE")]);
  });

  it("os mais pedidos não trazem números para a página", async () => {
    const menu = await loadMenu((await createLink()).token);

    expect(menu!.popular.slice(0, 2)).toEqual([id("CAFE"), id("PAO")]);
    expect(JSON.stringify(menu)).not.toMatch(/revenue|total_cents|orders/);
  });

  it("link revogado deixa de abrir, e só quem edita revoga", async () => {
    const link = await createLink("Mesa 9");

    const denied = await call(DELETE, "DELETE", viewerSession, undefined, `?id=${link.id}`);
    expect(denied.status).toBe(403);
    expect(await loadMenu(link.token)).not.toBeNull();

    const revoked = await call(DELETE, "DELETE", ownerSession, undefined, `?id=${link.id}`);
    expect(revoked.status).toBe(200);
    expect(await loadMenu(link.token)).toBeNull();
  });

  it("outro restaurante não revoga nem vê o QR daqui", async () => {
    const link = await createLink("Mesa 1");

    const revoke = await call(DELETE, "DELETE", otherSession, undefined, `?id=${link.id}`);
    expect(revoke.status).toBe(404);
    const qr = await QR(new Request(`http://localhost/api/panel/menu/qr?id=${link.id}`, {
      headers: { cookie: `erp_session=${otherSession}` },
    }));
    expect(qr.status).toBe(404);
    const list = await (await call(GET, "GET", otherSession)).json();
    expect(list.links.map((l: { id: string }) => l.id)).not.toContain(link.id);
  });

  it("restaurante suspenso tira o cardápio do ar", async () => {
    const link = await createLink();
    await admin`UPDATE tenants SET suspended_at = now() WHERE id = ${tenant}`;
    try {
      expect(await loadMenu(link.token)).toBeNull();
    } finally {
      await admin`UPDATE tenants SET suspended_at = NULL WHERE id = ${tenant}`;
    }
  });

  it("token inventado responde igual a token revogado", async () => {
    expect(await loadMenu("A".repeat(24))).toBeNull();
    expect(await loadMenu("../../etc/passwd")).toBeNull();
  });

  it("quem só consulta não cria QR, e a criação vai para a auditoria", async () => {
    const denied = await call(POST, "POST", viewerSession, { store_id: store });
    expect(denied.status).toBe(403);

    const link = await createLink("Varanda 2");
    const [event] = await admin<{ event_type: string; actor_user_id: string }[]>`
      SELECT event_type, actor_user_id::text FROM panel_admin_events
       WHERE tenant_id = ${tenant} AND payload_json->>'link_id' = ${link.id}`;
    expect(event).toEqual({ event_type: "menu_link_created", actor_user_id: owner });
  });

  it("loja de outro restaurante não recebe QR", async () => {
    const response = await call(POST, "POST", ownerSession, { store_id: otherStore });
    expect(response.status).toBe(404);
  });

  it("editar o produto no cardápio avança o server_seq, para o caixa receber a categoria", async () => {
    const [before] = await admin<{ server_seq: string }[]>`SELECT server_seq::text FROM products WHERE id = ${id("BOLO")}`;
    const response = await PUT_PRODUCT(new Request("http://localhost/api/panel/menu/products", {
      method: "PUT",
      headers: { cookie: `erp_session=${ownerSession}`, "content-type": "application/json" },
      body: JSON.stringify({ product_id: id("BOLO"), category: "  Doces   da casa ", description: "Com cobertura", menu_visible: true }),
    }));
    expect(response.status).toBe(200);

    const [after] = await admin<{ server_seq: string; category: string; description: string }[]>`
      SELECT server_seq::text, category, description FROM products WHERE id = ${id("BOLO")}`;
    expect(Number(after!.server_seq)).toBeGreaterThan(Number(before!.server_seq));
    expect(after!.category).toBe("Doces da casa");
    expect(after!.description).toBe("Com cobertura");
  });

  it("o preço não se muda pelo cardápio", async () => {
    const response = await PUT_PRODUCT(new Request("http://localhost/api/panel/menu/products", {
      method: "PUT",
      headers: { cookie: `erp_session=${ownerSession}`, "content-type": "application/json" },
      body: JSON.stringify({ product_id: id("CAFE"), category: "Bebidas", description: "", menu_visible: true, price_cents: 1 }),
    }));
    expect(response.status).toBe(422);
    const [row] = await admin<{ price_cents: string }[]>`SELECT price_cents::text FROM products WHERE id = ${id("CAFE")}`;
    expect(row!.price_cents).toBe("700");
  });

  it("o QR é um SVG com o endereço do cardápio", async () => {
    const link = await createLink("Mesa 7");
    const response = await QR(new Request(`http://localhost/api/panel/menu/qr?id=${link.id}`, {
      headers: { cookie: `erp_session=${viewerSession}` },
    }));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("image/svg+xml");
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(await response.text()).toContain("<svg");
  });
});
