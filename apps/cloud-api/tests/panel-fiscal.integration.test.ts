/**
 * Cadastro fiscal pelo painel, contra PostgreSQL real e pelo papel `erp_app`.
 *
 * O papel restrito não é detalhe: é ele que prova que os GRANTs e o RLS das
 * tabelas fiscais deixam o dono cadastrar — e que a auditoria em
 * `panel_admin_events` é gravável e não alterável por quem grava.
 */

import { createHash, randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const APP_URL = process.env.TEST_APP_DATABASE_URL;
const describeDb = ADMIN_URL && APP_URL ? describe : describe.skip;

const VALID_CNPJ = "11222333000181";

type Handler = (request: Request) => Promise<Response>;

describeDb("cadastro fiscal pelo painel", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let store: string;
  let product: string;
  let ownerCookie: string;
  let managerCookie: string;
  let GET: Handler;
  let PUT: Handler;
  let PUT_PRODUCT: Handler;

  async function session(role: "owner" | "manager"): Promise<string> {
    const user = randomUUID();
    await admin`INSERT INTO panel_users (id, tenant_id, email, name, role, password_hash, can_authorize)
                VALUES (${user}, ${tenant}, ${`${role}-${user}@teste.local`}, ${role}, ${role},
                        'hash-inutil', true)`;
    const token = `sessao-${role}-${randomUUID()}`;
    await admin`INSERT INTO panel_sessions (token_hash, tenant_id, user_id, expires_at)
                VALUES (${createHash("sha256").update(token).digest("hex")}, ${tenant}, ${user},
                        now() + interval '1 hour')`;
    return `erp_session=${token}`;
  }

  function request(method: string, path: string, cookie: string, body?: unknown): Request {
    return new Request(`http://localhost${path}`, {
      method,
      headers: { cookie, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  function storeConfig(overrides: Record<string, unknown> = {}) {
    return {
      store_id: store, uf: "RJ", environment: "homologation", cnpj: "11.222.333/0001-81",
      state_registration: "12.345.678", tax_regime: 1, legal_name: "Confeitaria Aurora Ltda",
      address: { street: "Rua do Ouvidor", number: "50", district: "Centro",
                 city_code: "3304557", city: "Rio de Janeiro", zip: "20040-030" },
      certificate_ref: "loja-centro/a1.pfx", csc_ref: "loja-centro/csc", csc_id: "1",
      enabled: true, normal_series: 1,
      ...overrides,
    };
  }

  function productProfile(overrides: Record<string, unknown> = {}) {
    return {
      product_id: product, ncm: "1905.90.90", cfop: "5102", unit_code: "un", origin: 0,
      csosn: "102", cst_icms: null, cst_pis: "49", cst_cofins: "49", ...overrides,
    };
  }

  beforeAll(async () => {
    process.env.DATABASE_URL = APP_URL!;
    process.env.SESSION_SECRET = "painel-fiscal-segredo-de-teste-32-chars";
    delete process.env.FISCAL_PRODUCTION_ENABLED;
    ({ GET, PUT } = await import("../src/app/api/panel/fiscal/route.ts"));
    ({ PUT: PUT_PRODUCT } = await import("../src/app/api/panel/fiscal/products/route.ts"));
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });

    const [t] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Fiscal Painel') RETURNING id`;
    tenant = t!.id;
    const [s] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${tenant}, 'Centro') RETURNING id`;
    store = s!.id;
    const [p] = await admin<{ id: string }[]>`
      INSERT INTO products (tenant_id, sku, name, price_cents) VALUES (${tenant}, 'TORTA', 'Torta', 8900)
      RETURNING id::text AS id`;
    product = p!.id;
    ownerCookie = await session("owner");
    managerCookie = await session("manager");
  });

  afterAll(async () => {
    if (!admin) return;
    await admin`SET session_replication_role = replica`;
    try {
      await admin`DELETE FROM panel_admin_events WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_documents WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_product_profiles WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_series WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_configurations WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM tenants WHERE id=${tenant}`;
    } finally {
      await admin`SET session_replication_role = origin`;
    }
    await admin.end({ timeout: 5 });
  });

  it("gerente não lê nem altera o cadastro fiscal", async () => {
    // O cadastro decide em nome de qual CNPJ as notas saem; a responsabilidade
    // tributária é do dono.
    expect((await GET(request("GET", `/api/panel/fiscal?store=${store}`, managerCookie))).status).toBe(403);
    expect((await PUT(request("PUT", "/api/panel/fiscal", managerCookie, storeConfig()))).status).toBe(403);
    expect((await PUT_PRODUCT(request("PUT", "/api/panel/fiscal/products", managerCookie, productProfile()))).status).toBe(403);
  });

  it("antes do cadastro, a tela diz o que falta para a primeira nota", async () => {
    const body = await (await GET(request("GET", `/api/panel/fiscal?store=${store}`, ownerCookie))).json();

    expect(body.config).toBeNull();
    expect(body.blockers.join(" ")).toContain("Cadastro fiscal da loja não preenchido");
    expect(body.blockers.join(" ")).toContain("Série normal");
    expect(body.blockers.join(" ")).toContain("1 produto(s) sem perfil");
  });

  it("o certificado e a senha nunca passam por esta API", async () => {
    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie,
      { ...storeConfig(), certificate_password: "123456" }));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toContain("nunca passam por esta tela");
    const [row] = await admin`SELECT 1 FROM fiscal_configurations WHERE tenant_id=${tenant}`;
    expect(row).toBeUndefined();
  });

  it("CNPJ com dígito errado é recusado no cadastro, não na venda", async () => {
    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie,
      storeConfig({ cnpj: "11222333000182" })));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toContain("CNPJ inválido");
  });

  it("produção é recusada enquanto o motor não foi homologado", async () => {
    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie,
      storeConfig({ environment: "production" })));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toContain("Produção ainda não liberada");
  });

  it("ligar a emissão sem as referências do cofre é recusado", async () => {
    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie,
      storeConfig({ certificate_ref: "", csc_ref: "", csc_id: "" })));

    expect(response.status).toBe(422);
    const [row] = await admin`SELECT 1 FROM fiscal_configurations WHERE tenant_id=${tenant}`;
    expect(row).toBeUndefined();
  });

  it("o dono cadastra a loja: normalizado, com série e auditado", async () => {
    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie, storeConfig()));
    expect(response.status).toBe(200);

    const [row] = await admin<{ cnpj: string; state_registration: string; enabled: boolean }[]>`
      SELECT cnpj, state_registration, enabled FROM fiscal_configurations WHERE tenant_id=${tenant}`;
    // Pontuação sai antes de gravar: a chave de acesso carrega só os dígitos.
    expect(row).toEqual({ cnpj: VALID_CNPJ, state_registration: "12345678", enabled: true });

    const [series] = await admin<{ series: number }[]>`
      SELECT series FROM fiscal_series WHERE tenant_id=${tenant} AND purpose='normal'`;
    expect(series!.series).toBe(1);

    const [event] = await admin<{ payload_json: Record<string, unknown> }[]>`
      SELECT payload_json FROM panel_admin_events
       WHERE tenant_id=${tenant} AND event_type='fiscal_config_updated'`;
    expect(event!.payload_json.certificate_ref).toBe("loja-centro/a1.pfx");
  });

  it("série que já emitiu não pode ser trocada", async () => {
    const device = randomUUID();
    await admin`INSERT INTO devices (id, tenant_id, store_id, token_hash)
                VALUES (${device}, ${tenant}, ${store}, ${randomUUID()})`;
    await admin`INSERT INTO fiscal_documents
      (tenant_id, store_id, device_id, order_id, request_uuid, series, number, status)
      VALUES (${tenant}, ${store}, ${device}, ${randomUUID()}, ${randomUUID()}, 1, 1, 'authorized')`;

    const response = await PUT(request("PUT", "/api/panel/fiscal", ownerCookie,
      storeConfig({ normal_series: 2 })));

    expect(response.status).toBe(409);
    expect((await response.json()).detail).toContain("já emitiu documentos");
  });

  it("perfil do produto: CFOP interestadual é recusado", async () => {
    const response = await PUT_PRODUCT(request("PUT", "/api/panel/fiscal/products", ownerCookie,
      productProfile({ cfop: "6102" })));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toContain("operação interna");
  });

  it("perfil do produto: a loja é do Simples, então CST de ICMS é recusado", async () => {
    const response = await PUT_PRODUCT(request("PUT", "/api/panel/fiscal/products", ownerCookie,
      productProfile({ csosn: null, cst_icms: "00" })));

    expect(response.status).toBe(422);
    expect((await response.json()).detail).toContain("use CSOSN");
  });

  it("o dono completa o produto e a tela deixa de apontar pendência nele", async () => {
    const response = await PUT_PRODUCT(request("PUT", "/api/panel/fiscal/products", ownerCookie,
      productProfile()));
    expect(response.status).toBe(200);

    const [row] = await admin<{ ncm: string; unit_code: string }[]>`
      SELECT ncm, unit_code FROM fiscal_product_profiles WHERE tenant_id=${tenant}`;
    expect(row).toEqual({ ncm: "19059090", unit_code: "UN" });

    const body = await (await GET(request("GET", `/api/panel/fiscal?store=${store}`, ownerCookie))).json();
    expect(body.products[0].complete).toBe(true);
    expect(body.blockers.join(" ")).not.toContain("sem perfil");

    const [event] = await admin`SELECT 1 FROM panel_admin_events
                                 WHERE tenant_id=${tenant} AND event_type='fiscal_profile_updated'`;
    expect(event).toBeDefined();
  });

  it("a auditoria do cadastro não pode ser alterada pela aplicação", async () => {
    const app = postgres(APP_URL!, { max: 1, onnotice: () => {} });
    try {
      await expect(app`UPDATE panel_admin_events SET payload_json='{}' WHERE tenant_id=${tenant}`)
        .rejects.toThrow();
    } finally {
      await app.end({ timeout: 5 });
    }
  });

  it("loja de outro tenant não é encontrada", async () => {
    const [other] = await admin<{ id: string }[]>`INSERT INTO tenants (name) VALUES ('Alheio') RETURNING id`;
    const [alien] = await admin<{ id: string }[]>`INSERT INTO stores (tenant_id, name) VALUES (${other!.id}, 'Alheia') RETURNING id`;
    try {
      const response = await GET(request("GET", `/api/panel/fiscal?store=${alien!.id}`, ownerCookie));
      expect(response.status).toBe(404);
    } finally {
      await admin`DELETE FROM stores WHERE id=${alien!.id}`;
      await admin`DELETE FROM tenants WHERE id=${other!.id}`;
    }
  });
});
