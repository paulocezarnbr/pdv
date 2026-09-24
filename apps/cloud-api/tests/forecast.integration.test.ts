/**
 * A previsão contra PostgreSQL real, com o papel `erp_app` (sem BYPASSRLS).
 *
 * Doze semanas de uma pizzaria: fecha às segundas, vende 2 pizzas por dia no
 * almoço e 6 no sábado — às 23h30, que em UTC já é domingo. Se o dia fosse
 * contado em UTC, a previsão diria que o movimento forte é no domingo.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

type Handler = (request: Request) => Promise<Response>;
type Forecast = import("../src/lib/forecast/load.ts").StoreForecast;

const TZ = "America/Sao_Paulo";
const SATURDAY = 6;
const MONDAY = 1;

describeDb("previsão de demanda contra PostgreSQL real", () => {
  let admin: postgres.Sql;
  let GET: Handler;
  let COUNT: Handler;
  let clearForecastCache: () => void;
  let localToday: (now: Date) => string;

  let tenant: string; let other: string; let store: string; let otherStore: string;
  let managerSession: string; let viewerSession: string; let otherSession: string;
  const flour = "insumo-farinha-0001";

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "forecast-integration-secret-32-chars-ok";
    ({ GET } = await import("../src/app/api/panel/forecast/route.ts"));
    ({ POST: COUNT } = await import("../src/app/api/panel/forecast/counts/route.ts"));
    ({ clearForecastCache, localToday } = await import("../src/lib/forecast/load.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const first = (rows: { id: string }[]): string => rows[0]!.id;
    tenant = first(await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Pizzaria Previsão') RETURNING id`);
    other = first(await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Pizzaria Rival') RETURNING id`);
    // "A Loja" vem antes de "Z" na ordem alfabética: é a loja padrão sem `?store`.
    store = first(await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'A Loja Centro') RETURNING id`);
    await admin`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Z Loja Nova')`;
    otherStore = first(await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${other}, 'Rival') RETURNING id`);

    managerSession = await session(tenant, "manager");
    viewerSession = await session(tenant, "viewer");
    otherSession = await session(other, "owner");

    const [pizza] = await admin<{ id: string }[]>`
      INSERT INTO products (tenant_id, sku, name, price_cents) VALUES (${tenant}, 'PIZZA', 'Pizza Margherita', 5000)
      RETURNING id::text`;

    const today = localToday(new Date());
    await sales(tenant, store, pizza!.id, "Pizza Margherita", today, { status: "paid" });
    // Nada disto conta: item cancelado, pedido aberto e outro restaurante.
    await sales(tenant, store, pizza!.id, "Pizza Margherita", today, { status: "paid", canceled: true });
    await sales(tenant, store, null, "Refrigerante", today, { status: "open" });
    await sales(other, otherStore, null, "Pizza da Rival", today, { status: "paid" });
  });

  beforeEach(() => clearForecastCache());

  afterAll(async () => {
    if (!admin) return;
    // A trilha é imutável por gatilho; só a limpeza do teste passa por cima.
    await admin`SET session_replication_role = replica`;
    for (const id of [tenant, other]) {
      await admin`DELETE FROM panel_admin_events WHERE tenant_id = ${id}`;
      await admin`DELETE FROM inventory_counts WHERE tenant_id = ${id}`;
      await admin`DELETE FROM stock_movements WHERE tenant_id = ${id}`;
      await admin`DELETE FROM order_item_ingredients WHERE tenant_id = ${id}`;
      await admin`DELETE FROM order_items WHERE tenant_id = ${id}`;
      await admin`DELETE FROM orders WHERE tenant_id = ${id}`;
      await admin`DELETE FROM panel_sessions WHERE tenant_id = ${id}`;
      await admin`DELETE FROM panel_users WHERE tenant_id = ${id}`;
      await admin`DELETE FROM products WHERE tenant_id = ${id}`;
      await admin`DELETE FROM stores WHERE tenant_id = ${id}`;
      await admin`DELETE FROM tenants WHERE id = ${id}`;
    }
    await admin`SET session_replication_role = origin`;
    await admin.end({ timeout: 5 });
  });

  /** 12 semanas até ontem: segunda fechada, sábado 6 pedidos às 23h30, resto 2 ao meio-dia. */
  async function sales(
    tenantId: string, storeId: string, productId: string | null, name: string, today: string,
    options: { status: string; canceled?: boolean },
  ) {
    const marker = randomUUID();
    await admin`
      INSERT INTO orders (id, tenant_id, store_id, device_id, client_uuid, local_number,
                          status, total_cents, opened_at, closed_at)
      SELECT gen_random_uuid(), ${tenantId}, ${storeId}, ${randomUUID()},
             ${marker} || ':' || day::text || ':' || i, i, ${options.status}, 5000, ts, ts
        FROM generate_series(${today}::date - 84, ${today}::date - 1, interval '1 day') AS d(day),
             LATERAL (SELECT CASE extract(dow FROM day)::int WHEN ${SATURDAY} THEN 6
                                  WHEN ${MONDAY} THEN 0 ELSE 2 END AS n) k,
             LATERAL generate_series(1, k.n) AS g(i),
             LATERAL (SELECT (day::date + CASE WHEN extract(dow FROM day)::int = ${SATURDAY}
                                               THEN time '23:30' ELSE time '12:00' END)
                             AT TIME ZONE ${TZ} AS ts) t
    `;
    await admin`
      INSERT INTO order_items (id, tenant_id, order_id, client_uuid, product_id, product_name,
                               quantity, total_cents, canceled_at)
      SELECT gen_random_uuid(), tenant_id, id, gen_random_uuid()::text, ${productId}, ${name},
             '1', 5000, ${options.canceled ? new Date() : null}
        FROM orders WHERE tenant_id = ${tenantId} AND client_uuid LIKE ${marker + ":%"}
    `;
    // 250 g de farinha por pizza.
    await admin`
      INSERT INTO order_item_ingredients (id, tenant_id, order_item_id, client_uuid,
                                          inventory_item_id, inventory_item_name, consumed_mg, unit_cost_cents)
      SELECT gen_random_uuid(), i.tenant_id, i.id, gen_random_uuid()::text,
             ${flour}, 'Farinha de trigo', 250000, 150
        FROM order_items i JOIN orders o ON o.id = i.order_id
       WHERE o.tenant_id = ${tenantId} AND o.client_uuid LIKE ${marker + ":%"}
    `;
  }

  async function session(tenantId: string, role: string): Promise<string> {
    const userId = randomUUID();
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
    return handler(new Request(`http://localhost/api/panel/forecast${query}`, {
      method,
      headers: { cookie: `erp_session=${cookie}`, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }));
  }

  async function forecastOf(cookie = managerSession, query = `?store=${store}`): Promise<Forecast> {
    const response = await call(GET, "GET", cookie, undefined, query);
    expect(response.status).toBe(200);
    return (await response.json()).forecast;
  }

  const weekday = (day: string) => new Date(`${day}T12:00:00Z`).getUTCDay();

  it("o jantar de sábado às 23h30 é venda de sábado, não de domingo", async () => {
    const result = await forecastOf();

    const pizza = result.products.find((p) => p.name === "Pizza Margherita")!;
    for (const { day, value } of pizza.days) {
      const expected = weekday(day) === SATURDAY ? 6 : weekday(day) === MONDAY ? 0 : 2;
      expect({ day, value }).toEqual({ day, value: expected });
    }
    expect(pizza.unit).toBe("un");
    expect(pizza.total).toBe(6 + 5 * 2);
    expect(pizza.method).toBe("sazonal");
  });

  it("só entra venda paga, item não cancelado e do próprio restaurante", async () => {
    const result = await forecastOf();

    expect(result.products.map((p) => p.name)).toEqual(["Pizza Margherita"]);
    // 72 dias abertos em 12 semanas: a segunda fechada não conta.
    expect(result.openDays).toBe(72);
  });

  it("o consumo de insumo sai da baixa real, e sem contagem não há sugestão de compra", async () => {
    const result = await forecastOf();

    const farinha = result.ingredients.find((i) => i.id === flour)!;
    expect(farinha.name).toBe("Farinha de trigo");
    // Uma semana: sábado 1,5 kg, segunda 0, cinco dias de 0,5 kg.
    expect(farinha.needKg).toBeCloseTo(4, 3);
    expect(farinha.balanceKg).toBeNull();
    expect(farinha.buyKg).toBeNull();
  });

  it("com a contagem, o saldo é a contagem mais o que se moveu depois, e a compra sai dele", async () => {
    const counted = await call(COUNT, "POST", managerSession, {
      store_id: store, inventory_item_id: flour, inventory_item_name: "Farinha de trigo", counted_kg: 1,
    });
    expect(counted.status).toBe(201);
    // Venda sincronizada DEPOIS da contagem baixa 250 g do saldo.
    await admin`
      INSERT INTO stock_movements (id, tenant_id, store_id, client_uuid, inventory_item_id,
                                   quantity_mg, reason, created_at)
      VALUES (${randomUUID()}, ${tenant}, ${store}, ${randomUUID()}, ${flour}, -250000, 'sale',
              now() + interval '1 second')`;
    // E uma de ANTES não conta: a contagem já a enxergou na prateleira.
    await admin`
      INSERT INTO stock_movements (id, tenant_id, store_id, client_uuid, inventory_item_id,
                                   quantity_mg, reason, created_at)
      VALUES (${randomUUID()}, ${tenant}, ${store}, ${randomUUID()}, ${flour}, -900000, 'sale',
              now() - interval '1 hour')`;
    clearForecastCache();

    const farinha = (await forecastOf()).ingredients.find((i) => i.id === flour)!;

    expect(farinha.balanceKg).toBeCloseTo(0.75, 3);
    expect(farinha.buyKg).toBeCloseTo(4 - 0.75, 3);
    expect(farinha.coverageDays).toBeCloseTo(0.75 / (4 / 7), 1);
    const [event] = await admin<{ n: number }[]>`
      SELECT count(*)::int AS n FROM panel_admin_events
       WHERE tenant_id = ${tenant} AND event_type = 'inventory_counted'`;
    expect(event!.n).toBe(1);
  });

  it("quem só consulta vê a previsão, mas não lança contagem", async () => {
    expect((await forecastOf(viewerSession)).products).toHaveLength(1);

    const response = await call(COUNT, "POST", viewerSession, {
      store_id: store, inventory_item_id: flour, inventory_item_name: "Farinha", counted_kg: 99,
    });

    expect(response.status).toBe(403);
  });

  it("outro restaurante não lê a previsão nem conta o estoque desta loja", async () => {
    const read = await call(GET, "GET", otherSession, undefined, `?store=${store}`);
    const write = await call(COUNT, "POST", otherSession, {
      store_id: store, inventory_item_id: flour, inventory_item_name: "Farinha", counted_kg: 99,
    });

    expect(read.status).toBe(404);
    expect(write.status).toBe(404);
    const counts = await admin<{ n: number }[]>`
      SELECT count(*)::int AS n FROM inventory_counts WHERE counted_mg = 99000000`;
    expect(counts[0]!.n).toBe(0);
  });

  it("sem loja escolhida, é a primeira do restaurante", async () => {
    const response = await call(GET, "GET", managerSession);
    const body = await response.json();

    expect(body.store.name).toBe("A Loja Centro");
    expect(body.can_count).toBe(true);
  });

  it("loja sem venda nenhuma não inventa previsão", async () => {
    const response = await call(GET, "GET", managerSession);
    const stores = await admin<{ id: string }[]>`
      SELECT id::text FROM stores WHERE tenant_id = ${tenant} AND name = 'Z Loja Nova'`;

    const empty = await forecastOf(managerSession, `?store=${stores[0]!.id}`);

    expect(response.status).toBe(200);
    expect(empty.products).toEqual([]);
    expect(empty.ingredients).toEqual([]);
    expect(empty.openDays).toBe(0);
  });

  it("contagem negativa ou absurda é recusada antes do banco", async () => {
    for (const counted_kg of [-1, 1_000_000]) {
      const response = await call(COUNT, "POST", managerSession, {
        store_id: store, inventory_item_id: flour, inventory_item_name: "Farinha", counted_kg,
      });
      expect(response.status).toBe(422);
    }
  });
});
