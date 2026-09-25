/**
 * Do painel ao caixa: o dono gera o código e o PDV o troca por token.
 *
 * Antes desta rota a ativação só existia nos testes, que inseriam o hash
 * direto no banco. Aqui o código sai da rota do painel e entra na rota de
 * ativação **como o PDV o envia** — sem os hífens que a tela mostra —, contra
 * PostgreSQL real e pelo papel restrito `erp_app`.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

type Handler = (request: Request) => Promise<Response>;

describeDb("código de ativação gerado pelo painel", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let store: string;
  let otherStore: string;
  let owner: string;
  let ownerCookie: string;
  let managerCookie: string;
  let viewerCookie: string;
  let ISSUE: Handler;
  let ACTIVATE: Handler;

  async function session(role: "owner" | "manager" | "viewer"): Promise<[string, string]> {
    const user = randomUUID();
    await admin`INSERT INTO panel_users (id, tenant_id, email, name, role, password_hash, can_authorize)
                VALUES (${user}, ${tenant}, ${`${role}-${user}@teste.local`}, ${role}, ${role},
                        'hash-inutil', true)`;
    const token = `sessao-${role}-${randomUUID()}`;
    await admin`INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at)
                VALUES (${createHash("sha256").update(token).digest("hex")}, ${tenant}, ${user},
                        now() + interval '1 hour')`;
    return [user, `erp_session=${token}`];
  }

  function issue(cookie: string, body: unknown): Promise<Response> {
    return ISSUE(new Request("http://localhost/api/panel/devices/activation-codes", {
      method: "POST",
      headers: { cookie, "content-type": "application/json", "x-forwarded-for": "203.0.113.7" },
      body: JSON.stringify(body),
    }));
  }

  function activate(code: string): Promise<Response> {
    // Um IP por chamada: o limitador de tentativas não é o assunto aqui.
    return ACTIVATE(new Request("http://localhost/api/devices/activate", {
      method: "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": `ativacao-${randomUUID()}` },
      body: JSON.stringify({
        activation_code: code,
        fingerprint: { hostname: "CAIXA-01", os: "Windows", arch: "AMD64" },
      }),
    }));
  }

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "ativacao-painel-segredo-de-teste-32ch";
    ({ POST: ISSUE } = await import("../src/app/api/panel/devices/activation-codes/route.ts"));
    ({ POST: ACTIVATE } = await import("../src/app/api/devices/activate/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Ativação Painel') RETURNING id`;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Pool Bar') RETURNING id`;
    store = s!.id;
    const [other] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Outro tenant') RETURNING id`;
    const [os] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${other!.id}, 'Loja alheia') RETURNING id`;
    otherStore = os!.id;
    [owner, ownerCookie] = await session("owner");
    [, managerCookie] = await session("manager");
    [, viewerCookie] = await session("viewer");
  });

  afterAll(async () => {
    if (!admin) return;
    await admin`SET session_replication_role = replica`;
    try {
      await admin`DELETE FROM panel_admin_events WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM device_secrets WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM tenants WHERE id=${tenant} OR name='Outro tenant'`;
    } finally {
      await admin`SET session_replication_role = origin`;
    }
    await admin.end({ timeout: 5 });
  });

  it("o dono gera o código e o PDV, digitando sem hífen, ativa o terminal", async () => {
    const response = await issue(ownerCookie, { store_id: store, label: "Caixa 1" });
    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toBe("no-store");
    const body = await response.json();
    expect(body.code).toMatch(/^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$/);
    expect(body.store_name).toBe("Pool Bar");
    expect(body.ttl_minutes).toBe(15);

    // O PDV em Python descarta o que não é letra ou número, e o técnico pode
    // digitar em minúsculas.
    const typed = body.code.replace(/-/g, "").toLowerCase();
    const activated = await activate(typed);
    expect(activated.status).toBe(200);
    const result = await activated.json();
    expect(result.tenant_id).toBe(tenant);
    expect(result.store_id).toBe(store);
    expect(result.device_id).toBe(body.device_id);
    expect(result.store_name).toBe("Pool Bar");

    const [device] = await admin<{ label: string; hostname: string }[]>`
      SELECT label, hostname FROM devices WHERE id=${body.device_id}`;
    expect(device).toEqual({ label: "Caixa 1", hostname: "CAIXA-01" });
  });

  it("o mesmo código não ativa um segundo terminal", async () => {
    const { code } = await (await issue(ownerCookie, { store_id: store, label: "Caixa 2" })).json();
    expect((await activate(code)).status).toBe(200);
    expect((await activate(code)).status).toBe(410);
  });

  it("o banco e a auditoria guardam o hash e o autor, nunca o código", async () => {
    const { code, device_id } = await (await issue(ownerCookie, { store_id: store, label: "Caixa 3" })).json();
    const stored = await admin<{ code_hash: string }[]>`
      SELECT code_hash FROM device_activation_codes WHERE device_id=${device_id}`;
    expect(stored).toHaveLength(1);
    expect(stored[0]!.code_hash).not.toContain(code.replace(/-/g, ""));

    const [event] = await admin<{ actor_user_id: string; payload_json: Record<string, unknown>; ip: string }[]>`
      SELECT actor_user_id, payload_json, ip FROM panel_admin_events
       WHERE tenant_id=${tenant} AND event_type='activation_code_issued'
         AND payload_json->>'device_id'=${device_id}`;
    expect(event!.actor_user_id).toBe(owner);
    expect(event!.payload_json).toEqual({ store_id: store, device_id, label: "Caixa 3" });
    expect(event!.ip).toBe("203.0.113.7");
    expect(JSON.stringify(event)).not.toContain(code.replace(/-/g, ""));
  });

  it("o gerente também ativa; quem só visualiza, não", async () => {
    expect((await issue(managerCookie, { store_id: store, label: "Caixa 4" })).status).toBe(201);
    expect((await issue(viewerCookie, { store_id: store, label: "Caixa 5" })).status).toBe(403);
  });

  it("loja de outro tenant não existe para este painel", async () => {
    const response = await issue(ownerCookie, { store_id: otherStore, label: "Invasor" });
    expect(response.status).toBe(404);
    const [row] = await admin<{ total: string }[]>`
      SELECT count(*)::text AS total FROM device_activation_codes WHERE store_id=${otherStore}`;
    expect(row!.total).toBe("0");
  });

  it("sem sessão, não há código", async () => {
    const response = await ISSUE(new Request("http://localhost/api/panel/devices/activation-codes", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ store_id: store, label: "Caixa" }),
    }));
    expect(response.status).toBe(401);
  });

  it("código vencido é recusado com a mesma mensagem de inexistente", async () => {
    const { code, device_id } = await (await issue(ownerCookie, { store_id: store, label: "Caixa 6" })).json();
    await admin`UPDATE device_activation_codes SET created_at = now() - interval '16 minutes'
                 WHERE device_id=${device_id}`;
    const expired = await activate(code);
    expect(expired.status).toBe(410);
    const unknown = await activate("ZZZZ-ZZZZ-ZZZZ");
    expect((await expired.json()).detail).toBe((await unknown.json()).detail);
  });
});
