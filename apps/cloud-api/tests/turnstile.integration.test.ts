/**
 * Login do painel com o Cloudflare Turnstile, contra PostgreSQL real.
 *
 * A Cloudflare é simulada no `fetch`: o que se prova é o que o servidor faz
 * com cada resposta dela — e que robô barrado não gasta tentativa da conta.
 */

import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

type Handler = (request: Request) => Promise<Response>;

describeDb("Cloudflare Turnstile no login do painel", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let email: string;
  let LOGIN: Handler;
  let CONFIG: Handler;
  const password = `senha-de-teste-${randomUUID()}`;
  const cloudflare: { body?: URLSearchParams; answer: () => Promise<Response> } = {
    answer: async () => Response.json({ success: true, action: "login" }),
  };

  function login(body: Record<string, unknown>, ip = `turnstile-${randomUUID()}`): Promise<Response> {
    return LOGIN(new Request("http://localhost/api/panel/session", {
      method: "POST",
      headers: { "content-type": "application/json", "x-forwarded-for": ip },
      body: JSON.stringify(body),
    }));
  }

  async function throttleRows(): Promise<number> {
    const [row] = await admin<{ total: string }[]>`
      SELECT count(*)::text AS total FROM panel_login_throttle WHERE scope LIKE ${`%${email}%`}`;
    return Number(row!.total);
  }

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "turnstile-segredo-de-teste-com-32-chars";
    ({ POST: LOGIN } = await import("../src/app/api/panel/session/route.ts"));
    ({ GET: CONFIG } = await import("../src/app/api/panel/login-config/route.ts"));
    const { hashPassword } = await import("../src/lib/auth/panel.ts");
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Turnstile') RETURNING id`;
    tenant = t!.id;
    email = `dono-${randomUUID()}@turnstile.test`;
    await admin`INSERT INTO panel_users (tenant_id, email, name, role, password_hash, can_authorize)
                VALUES (${tenant}, ${email}, 'Dono', 'owner', ${await hashPassword(password)}, true)`;

    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      expect(url).toBe("https://challenges.cloudflare.com/turnstile/v0/siteverify");
      cloudflare.body = init?.body as URLSearchParams;
      return cloudflare.answer();
    });
  });

  afterEach(() => {
    delete process.env.TURNSTILE_SECRET_KEY;
    delete process.env.TURNSTILE_SITE_KEY;
    cloudflare.body = undefined;
    cloudflare.answer = async () => Response.json({ success: true, action: "login" });
  });

  afterAll(async () => {
    vi.unstubAllGlobals();
    if (!admin) return;
    await admin`DELETE FROM panel_login_throttle WHERE scope LIKE ${`%${email}%`}`;
    await admin`DELETE FROM panel_sessions WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM panel_users WHERE tenant_id=${tenant}`;
    await admin`DELETE FROM tenants WHERE id=${tenant}`;
    await admin.end({ timeout: 5 });
  });

  it("desligado, o login funciona como antes e a tela não pede captcha", async () => {
    expect((await login({ email, password })).status).toBe(200);
    const config = await (await CONFIG(new Request("http://localhost/api/panel/login-config"))).json();
    expect(config).toEqual({ turnstile_site_key: null, turnstile_required: false });
  });

  it("ligado, a tela recebe só a chave pública", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo-que-nao-pode-vazar";
    process.env.TURNSTILE_SITE_KEY = "0x4AAAAAAA-chave-publica";
    const response = await CONFIG(new Request("http://localhost/api/panel/login-config"));
    const text = await response.text();
    expect(JSON.parse(text)).toEqual({ turnstile_site_key: "0x4AAAAAAA-chave-publica", turnstile_required: true });
    expect(text).not.toContain("segredo-que-nao-pode-vazar");
  });

  it("sem o token, o login é recusado antes de conferir a senha", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    const response = await login({ email, password });
    expect(response.status).toBe(400);
    expect(cloudflare.body).toBeUndefined();
    expect(await throttleRows()).toBe(0);
  });

  it("token aceito pela Cloudflare: entra, e o segredo e o IP foram conferidos lá", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    const response = await login({ email, password, turnstile_token: "token-bom" }, "203.0.113.9");
    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toContain("erp_session=");
    expect(cloudflare.body?.get("secret")).toBe("segredo");
    expect(cloudflare.body?.get("response")).toBe("token-bom");
    expect(cloudflare.body?.get("remoteip")).toBe("203.0.113.9");
  });

  it("token recusado: 403, e o robô não gasta tentativa da conta", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    cloudflare.answer = async () => Response.json({ success: false, "error-codes": ["invalid-input-response"] });
    for (let i = 0; i < 12; i++) {
      expect((await login({ email, password: "errada", turnstile_token: "token-ruim" })).status).toBe(403);
    }
    expect(await throttleRows()).toBe(0);
    // O dono continua entrando: o captcha não virou negação de serviço da conta.
    cloudflare.answer = async () => Response.json({ success: true, action: "login" });
    expect((await login({ email, password, turnstile_token: "token-bom" })).status).toBe(200);
  });

  it("token resolvido para outra ação não vale no login", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    cloudflare.answer = async () => Response.json({ success: true, action: "cardapio" });
    expect((await login({ email, password, turnstile_token: "token-de-outra-tela" })).status).toBe(403);
  });

  it("Cloudflare fora do ar: falha fechada", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    cloudflare.answer = async () => { throw new TypeError("fetch failed"); };
    expect((await login({ email, password, turnstile_token: "token-bom" })).status).toBe(503);
  });

  it("com o token certo, a senha errada ainda é senha errada", async () => {
    process.env.TURNSTILE_SECRET_KEY = "segredo";
    const response = await login({ email, password: "errada", turnstile_token: "token-bom" });
    expect(response.status).toBe(401);
    expect(await throttleRows()).toBeGreaterThan(0);
  });
});
