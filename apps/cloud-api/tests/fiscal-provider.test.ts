import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FiscalProviderUnavailable,
  HttpFiscalProvider,
  type FiscalIntent,
} from "../src/lib/fiscal/provider.ts";

const intent: FiscalIntent = {
  documentId: "doc", requestUuid: "request", orderId: "order",
  tenantId: "tenant", storeId: "store", deviceId: "device",
  model: 65, series: 1, number: 7, environment: "homologation",
  certificateRef: "loja/a1", cscRef: "loja/csc", cscId: "1",
  issuer: { uf: "RJ", cnpj: "00000000000000", stateRegistration: "123",
    taxRegime: 1, legalName: "Loja", address: {} },
  totalCents: 700,
  items: [{ productId: "p", name: "Cafe", quantity: "1", unitPriceCents: 700,
    totalCents: 700, ncm: "21011200", cfop: "5102", unitCode: "UN",
    origin: 0, csosn: "102", cstPis: "49", cstCofins: "49" }],
  payments: [{ method: "cash", amountCents: 1000, changeCents: 300 }],
};

afterEach(() => vi.unstubAllGlobals());

describe("adaptador do serviço fiscal interno", () => {
  it("envia só referência do segredo, nunca certificado ou senha", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      status: "authorized", code: "100", reason: "Autorizado",
      access_key: "33".padEnd(44, "0"), protocol: "123",
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    const result = await new HttpFiscalProvider({
      baseUrl: "http://fiscal:8081/", token: "segredo-interno",
    }).authorize(intent);

    expect(result.status).toBe("authorized");
    const [, options] = fetch.mock.calls[0]!;
    expect(JSON.parse(String(options.body))).toMatchObject({ certificateRef: "loja/a1" });
    expect(String(options.body)).not.toContain("password");
    expect(options.headers.authorization).toBe("Bearer segredo-interno");
  });

  it("transforma timeout em resultado ambíguo, não em rejeição definitiva", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("timeout", "AbortError")));
    await expect(new HttpFiscalProvider({ baseUrl: "http://fiscal", token: "x" })
      .authorize(intent)).rejects.toBeInstanceOf(FiscalProviderUnavailable);
  });

  it("recusa resposta de formato desconhecido", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", {
      status: 200, headers: { "content-type": "application/json" },
    })));
    await expect(new HttpFiscalProvider({ baseUrl: "http://fiscal", token: "x" })
      .authorize(intent)).rejects.toThrow("Resposta fiscal inválida");
  });

  it("não devolve corpo de erro potencialmente sensível", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("senha-do-a1", { status: 500 })));
    await expect(new HttpFiscalProvider({ baseUrl: "http://fiscal", token: "x" })
      .authorize(intent)).rejects.not.toThrow("senha-do-a1");
  });
});
