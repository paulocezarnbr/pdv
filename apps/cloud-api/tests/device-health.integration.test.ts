/**
 * O estado operacional do terminal chega ao painel.
 *
 * O caso que motivou: nenhum terminal real sincronizava, e o painel mostrava o
 * terminal "online" com o faturamento vazio. Agora a fila presa, a quarentena
 * e o relógio errado aparecem na linha do terminal.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { deviceIssues, formatDrift, queueSummary, type DeviceTelemetry } from "../src/lib/device-health.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

type Handler = (request: Request) => Promise<Response>;

describeDb("saúde do terminal", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let store: string;
  const device = randomUUID();
  const token = `token-${randomUUID()}`;
  let ownerCookie: string;
  let HEARTBEAT: Handler;
  let DASHBOARD: Handler;

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "saude-do-terminal-segredo-de-teste-32";
    ({ POST: HEARTBEAT } = await import("../src/app/api/devices/heartbeat/route.ts"));
    ({ GET: DASHBOARD } = await import("../src/app/api/panel/dashboard/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Saude') RETURNING id`;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Centro') RETURNING id`;
    store = s!.id;
    await admin`INSERT INTO devices (id, tenant_id, store_id, label, token_hash)
                VALUES (${device}, ${tenant}, ${store}, 'Caixa 1',
                        ${createHash("sha256").update(token).digest("hex")})`;

    const user = randomUUID();
    await admin`INSERT INTO panel_users (id, tenant_id, email, name, role, password_hash, can_authorize)
                VALUES (${user}, ${tenant}, ${`dono-${user}@teste.local`}, 'Dono', 'owner', 'x', true)`;
    const session = `sessao-${randomUUID()}`;
    await admin`INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at)
                VALUES (${createHash("sha256").update(session).digest("hex")}, ${tenant}, ${user},
                        now() + interval '1 hour')`;
    ownerCookie = `erp_session=${session}`;
  });

  afterAll(async () => {
    if (!admin) return;
    await admin`DELETE FROM tenants WHERE id=${tenant}`;
    await admin.end({ timeout: 5 });
  });

  function beat(body: Record<string, unknown>, bearer = token): Promise<Response> {
    return HEARTBEAT(new Request("http://localhost/api/devices/heartbeat", {
      method: "POST",
      headers: { authorization: `Bearer ${bearer}`, "content-type": "application/json" },
      body: JSON.stringify({
        device_id: device, tenant_id: tenant, terminal_clock: new Date().toISOString(),
        pending_items: 0, quarantined_items: 0, oldest_pending_at: null,
        last_quarantine_reason: null, ...body,
      }),
    }));
  }

  async function panelDevice() {
    const response = await DASHBOARD(new Request(
      `http://localhost/api/panel/dashboard?from=${new Date(Date.now() - 86_400_000).toISOString()}` +
        `&to=${new Date().toISOString()}`,
      { headers: { cookie: ownerCookie } },
    ));
    expect(response.status).toBe(200);
    const body = await response.json();
    return body.devices.find((d: { id: string }) => d.id === device);
  }

  it("fila presa, quarentena e relógio chegam à linha do terminal", async () => {
    const response = await beat({
      pending_items: 42,
      quarantined_items: 3,
      oldest_pending_at: new Date(Date.now() - 40 * 60_000).toISOString(),
      last_quarantine_reason: "orders#01a0: 23502 local_number nulo",
      terminal_clock: new Date(Date.now() - 10 * 60_000).toISOString(),
    });
    expect(response.status).toBe(200);
    // O desvio é medido pela nuvem, contra o relógio dela.
    expect(Math.round((await response.json()).clock_drift_ms / 60_000)).toBe(-10);

    const row = await panelDevice();
    expect(row.queue_stuck).toBe(true);
    expect(row.clock_skewed).toBe(true);
    expect(deviceIssues(row).map((i) => i.text)).toEqual([
      "42 venda(s) presa(s) na fila há 40 min",
      "3 em quarentena: orders#01a0: 23502 local_number nulo",
      "relógio do caixa -10 min",
    ]);
  });

  it("terminal em dia não assusta ninguém", async () => {
    await beat({ pending_items: 2, oldest_pending_at: new Date().toISOString() });

    const row = await panelDevice();
    expect(deviceIssues(row)).toEqual([]);
    expect(queueSummary(row)).toMatch(/^2 na fila · relato há \d+s$/);
  });

  it("o relato é sobrescrito, não acumulado", async () => {
    await beat({ pending_items: 5 });
    await beat({ pending_items: 0 });

    const [row] = await admin<{ count: string }[]>`
      SELECT count(*) FROM device_telemetry WHERE device_id=${device}`;
    expect(Number(row!.count)).toBe(1);
    expect((await panelDevice()).pending_items).toBe(0);
  });

  it("um terminal não relata em nome de outro", async () => {
    const response = await beat({ device_id: randomUUID() });
    expect(response.status).toBe(403);
  });

  it("sem token de terminal, nada entra", async () => {
    const response = await beat({}, "token-falso");
    expect(response.status).toBe(401);
  });

  it("número absurdo é recusado na entrada", async () => {
    expect((await beat({ pending_items: -1 })).status).toBe(422);
    expect((await beat({ terminal_clock: "ontem" })).status).toBe(422);
  });
});

describe("como o painel lê a saúde", () => {
  const base: DeviceTelemetry = {
    reported_at: new Date().toISOString(), pending_items: 0, quarantined_items: 0,
    oldest_pending_at: null, clock_drift_ms: "0", last_quarantine_reason: null,
    queue_stuck: false, clock_skewed: false,
  };

  it("terminal que nunca relatou não é dado como saudável", () => {
    expect(deviceIssues({ ...base, reported_at: null })[0]!.text).toContain("sem relato");
  });

  it("o desvio diz se o caixa está adiantado ou atrasado", () => {
    expect(formatDrift(90_000)).toBe("+90 s");
    expect(formatDrift(-600_000)).toBe("-10 min");
    expect(formatDrift(3 * 3_600_000)).toBe("+3 h");
  });

  it("quarentena sem fila presa ainda é problema", () => {
    const issues = deviceIssues({ ...base, quarantined_items: 1 });
    expect(issues).toEqual([{ tone: "danger", text: "1 em quarentena" }]);
  });
});
