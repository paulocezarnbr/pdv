import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { DeviceContext } from "../src/lib/auth/device.ts";
import {
  FiscalProviderUnavailable,
  type FiscalIntent,
  type FiscalProvider,
  type FiscalProviderResult,
} from "../src/lib/fiscal/provider.ts";
import {
  fiscalDocumentStatus,
  issueFiscalDocument,
  type FiscalDocumentOut,
  type FiscalNotRequiredOut,
} from "../src/lib/fiscal/service.ts";
import { ApiError } from "../src/lib/http.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const describeDb = ADMIN_URL ? describe : describe.skip;

// O serviço fiscal usa o pool da aplicação (`withTenant`), que lê
// `DATABASE_URL`. Sem esta linha o teste dependia de a variável já estar no
// ambiente de quem rodou — e falhava com "variável obrigatória ausente" para
// qualquer outra pessoa, mesmo com `TEST_DATABASE_URL` definida.
//
// O papel restrito é preferido de propósito: é como a aplicação roda em
// produção, e é o único jeito de este teste provar que os GRANTs e o RLS das
// tabelas fiscais (migration 014) deixam o fluxo passar. Conectado como
// superusuário, uma permissão faltando passaria despercebida.
process.env.DATABASE_URL ??= process.env.TEST_APP_DATABASE_URL ?? ADMIN_URL;

class FakeProvider {
  calls = 0;
  constructor(private readonly result: FiscalProviderResult | Error) {}
  async authorize(_intent: FiscalIntent): Promise<FiscalProviderResult> {
    this.calls += 1;
    if (this.result instanceof Error) throw this.result;
    return this.result;
  }
  async query(_requestUuid: string): Promise<FiscalProviderResult> {
    if (this.result instanceof Error) throw this.result;
    return this.result;
  }
}

/**
 * Provedor com respostas separadas para `authorize` e `query`.
 *
 * A reconciliação depende exatamente dessa separação: a consulta diz se a
 * solicitação chegou ao serviço fiscal, e só então a autorização é (ou não é)
 * chamada de novo. Um dublê que responde igual aos dois não consegue expressar
 * "o serviço nunca viu isto, mas autorizaria se recebesse".
 */
class ScriptedProvider {
  authorizeCalls = 0;
  queryCalls = 0;
  constructor(
    private readonly onAuthorize: FiscalProviderResult | Error,
    private readonly onQuery: FiscalProviderResult | Error,
  ) {}
  async authorize(_intent: FiscalIntent): Promise<FiscalProviderResult> {
    this.authorizeCalls += 1;
    if (this.onAuthorize instanceof Error) throw this.onAuthorize;
    return this.onAuthorize;
  }
  async query(_requestUuid: string): Promise<FiscalProviderResult> {
    this.queryCalls += 1;
    if (this.onQuery instanceof Error) throw this.onQuery;
    return this.onQuery;
  }
}

const AUTHORIZED: FiscalProviderResult = {
  status: "authorized", code: "100", reason: "Autorizado",
  accessKey: "5".repeat(44), protocol: "789",
};
const NOT_FOUND: FiscalProviderResult = {
  status: "unknown", code: "NOT_FOUND", reason: "Solicitação nunca recebida.",
};
const IN_FLIGHT: FiscalProviderResult = {
  status: "unknown", code: "IN_FLIGHT", reason: "Iniciada e não concluída.",
};

describeDb("emissão fiscal contra PostgreSQL real", () => {
  let admin: postgres.Sql;
  let tenant: string;
  let store: string;
  let device: string;
  let order: string;
  let secondOrder: string;
  let context: DeviceContext;

  beforeAll(async () => {
    admin = postgres(ADMIN_URL!, { max: 1, onnotice: () => {} });
    tenant = randomUUID(); store = randomUUID(); device = randomUUID();
    order = randomUUID(); secondOrder = randomUUID();
    await admin`INSERT INTO tenants(id,name) VALUES (${tenant},'Fiscal Test')`;
    await admin`INSERT INTO stores(id,tenant_id,name) VALUES (${store},${tenant},'Loja')`;
    await admin`INSERT INTO devices(id,tenant_id,store_id,token_hash)
                VALUES (${device},${tenant},${store},${randomUUID()})`;
    await admin`INSERT INTO fiscal_configurations
      (tenant_id,store_id,environment,certificate_ref,csc_ref,csc_id,cnpj,
       state_registration,tax_regime,legal_name,enabled)
      VALUES (${tenant},${store},'homologation','loja/a1.pfx','loja/csc','1',
              '12345678000190','123',1,'Loja Teste',true)`;
    await admin`INSERT INTO fiscal_series(tenant_id,store_id,model,series,purpose)
                VALUES (${tenant},${store},65,1,'normal')`;
    await admin`INSERT INTO fiscal_product_profiles
      (tenant_id,product_id,ncm,cfop,unit_code,origin,csosn,cst_pis,cst_cofins)
      VALUES (${tenant},'p1','21011200','5102','UN',0,'102','49','49')`;
    for (const id of [order, secondOrder]) {
      await admin`INSERT INTO orders
        (id,tenant_id,store_id,device_id,client_uuid,local_number,status,total_cents)
        VALUES (${id},${tenant},${store},${device},${randomUUID()},1,'paid',700)`;
      await admin`INSERT INTO order_items
        (id,tenant_id,order_id,client_uuid,product_id,product_name,quantity,
         unit_price_cents,total_cents,created_at)
        VALUES (${randomUUID()},${tenant},${id},${randomUUID()},'p1','Cafe','1',700,700,now())`;
      await pay(id, 1000, 300);
    }
    context = { tenantId: tenant, storeId: store, deviceId: device, storeName: "Loja" };
  });

  /** O pagamento da venda, como o terminal sincroniza: valor entregue e troco. */
  async function pay(orderId: string, amountCents: number, changeCents = 0, method = "cash"): Promise<void> {
    await admin`INSERT INTO payments (id,tenant_id,order_id,client_uuid,method,amount_cents,change_cents,created_at)
                VALUES (${randomUUID()},${tenant},${orderId},${randomUUID()},${method},${amountCents},${changeCents},now())`;
  }

  /** Uma venda paga nova, para o teste não disputar pedido com os outros. */
  async function paidOrder(options: { pay?: boolean } = {}): Promise<string> {
    const id = randomUUID();
    await admin`INSERT INTO orders
      (id,tenant_id,store_id,device_id,client_uuid,local_number,status,total_cents)
      VALUES (${id},${tenant},${store},${device},${randomUUID()},1,'paid',700)`;
    await admin`INSERT INTO order_items
      (id,tenant_id,order_id,client_uuid,product_id,product_name,quantity,
       unit_price_cents,total_cents,created_at)
      VALUES (${randomUUID()},${tenant},${id},${randomUUID()},'p1','Cafe','1',700,700,now())`;
    if (options.pay ?? true) await pay(id, 700);
    return id;
  }

  async function nextNumber(): Promise<number> {
    const [series] = await admin<{ next_number: string }[]>`
      SELECT next_number FROM fiscal_series WHERE tenant_id=${tenant}`;
    return Number(series!.next_number);
  }

  /**
   * Reserva um número e deixa o documento `processing`, como se o processo
   * tivesse caído entre reservar e transmitir. Um erro que não é
   * `FiscalProviderUnavailable` é relançado por `transmit` sem gravar
   * resultado — é o mesmo efeito de o contêiner morrer naquele instante.
   */
  async function stuckDocument(): Promise<{ request: string; number: number }> {
    const request = randomUUID();
    const crash = new ScriptedProvider(new Error("processo caiu"), NOT_FOUND);
    await expect(
      issueFiscalDocument(context, request, await paidOrder(), crash),
    ).rejects.toThrow("processo caiu");
    const [row] = await admin<{ status: string; number: string }[]>`
      SELECT status, number FROM fiscal_documents
       WHERE tenant_id=${tenant} AND request_uuid=${request}::uuid`;
    expect(row!.status).toBe("processing");
    return { request, number: Number(row!.number) };
  }

  const later = () => new Date(Date.now() + 60_000);

  /** Estreita o resultado: nos testes de emissão, a venda TEM valor. */
  function issued(doc: FiscalDocumentOut | FiscalNotRequiredOut): FiscalDocumentOut {
    if (doc.status === "not_required") throw new Error("esperava documento emitido");
    return doc as FiscalDocumentOut;
  }

  /** Venda paga com o total escolhido — zero é a cortesia de 100%. */
  async function orderWithTotal(itemCents: number, totalCents: number): Promise<string> {
    const id = randomUUID();
    await admin`INSERT INTO orders
      (id,tenant_id,store_id,device_id,client_uuid,local_number,status,
       subtotal_cents,discount_cents,total_cents)
      VALUES (${id},${tenant},${store},${device},${randomUUID()},1,'paid',
              ${itemCents},${itemCents - totalCents},${totalCents})`;
    await admin`INSERT INTO order_items
      (id,tenant_id,order_id,client_uuid,product_id,product_name,quantity,
       unit_price_cents,total_cents,created_at)
      VALUES (${randomUUID()},${tenant},${id},${randomUUID()},'p1','Cafe','1',
              ${itemCents},${itemCents},now())`;
    if (totalCents > 0) await pay(id, totalCents);
    return id;
  }

  afterAll(async () => {
    if (!admin) return;
    // A imutabilidade fiscal vale inclusive para o administrador. Só o banco
    // descartável do teste desliga triggers para remover sua fixture.
    await admin`SET session_replication_role = replica`;
    try {
      await admin`DELETE FROM fiscal_events WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_documents WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_product_profiles WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_series WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM fiscal_configurations WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM payments WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM order_items WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM orders WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM tenants WHERE id=${tenant}`;
    } finally {
      await admin`SET session_replication_role = origin`;
    }
    await admin.end({ timeout: 5 });
  });

  it("a loja tem uma série normal só, e cada terminal a sua de contingência", async () => {
    // Guarda a regra que era `UNIQUE NULLS NOT DISTINCT` (PostgreSQL 15+) e
    // virou índice parcial para o banco 14 também migrar.
    await expect(admin`INSERT INTO fiscal_series(tenant_id,store_id,model,series,purpose)
                       VALUES (${tenant},${store},65,2,'normal')`).rejects.toMatchObject({ code: "23505" });

    await admin`INSERT INTO fiscal_series(tenant_id,store_id,device_id,model,series,purpose)
                VALUES (${tenant},${store},${device},65,900,'offline_contingency')`;
    await expect(admin`INSERT INTO fiscal_series(tenant_id,store_id,device_id,model,series,purpose)
                       VALUES (${tenant},${store},${device},65,901,'offline_contingency')`).rejects.toMatchObject({ code: "23505" });
    await admin`DELETE FROM fiscal_series WHERE tenant_id=${tenant} AND purpose='offline_contingency'`;
  });

  it("repete a mesma solicitação sem chamar o provedor ou consumir número", async () => {
    const provider = new FakeProvider({ status: "authorized", code: "100",
      reason: "Autorizado", accessKey: "3".repeat(44), protocol: "123" });
    const request = randomUUID();
    const first = await issueFiscalDocument(context, request, order, provider);
    const repeated = await issueFiscalDocument(context, request, order, provider);
    expect(issued(first.document).number).toBe(1);
    expect(issued(repeated.document).document_id).toBe(issued(first.document).document_id);
    expect(provider.calls).toBe(1);
    const [series] = await admin<{ next_number: string }[]>`
      SELECT next_number FROM fiscal_series WHERE tenant_id=${tenant}`;
    expect(Number(series!.next_number)).toBe(2);
  });

  it("timeout ambíguo vira unknown e nunca dispara contingência ou reenvio", async () => {
    const provider = new FakeProvider(new FiscalProviderUnavailable());
    const request = randomUUID();
    const first = await issueFiscalDocument(context, request, secondOrder, provider);
    const repeated = await issueFiscalDocument(context, request, secondOrder, provider);
    expect(first.document.status).toBe("unknown");
    expect(repeated.document.status).toBe("unknown");
    expect(provider.calls).toBe(1);

    const reconciler = new FakeProvider({ status: "authorized", code: "100",
      reason: "Autorizado após consulta", accessKey: "4".repeat(44), protocol: "456" });
    const reconciled = await fiscalDocumentStatus(context, request, reconciler);
    expect(reconciled.status).toBe("authorized");
    expect(reconciled.access_key).toBe("4".repeat(44));
  });
  it("documento preso que nunca chegou ao serviço é retransmitido com o MESMO número", async () => {
    // Antes da reconciliação ele ficava `processing` para sempre: o serviço
    // fiscal respondia `unknown` tanto para "nunca recebi" quanto para "recebi
    // e não concluí", e a nuvem não tinha como saber que era seguro reenviar.
    const { request, number } = await stuckDocument();
    const before = await nextNumber();

    const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);
    const reconciled = await fiscalDocumentStatus(context, request, provider, later());

    expect(reconciled.status).toBe("authorized");
    expect(reconciled.number).toBe(number);
    expect(provider.authorizeCalls).toBe(1);
    // Nenhum número novo: a retransmissão reaproveita a reserva original.
    expect(await nextNumber()).toBe(before);
  });

  it("solicitação que o serviço recebeu e não concluiu NÃO é retransmitida", async () => {
    // O motor pode ter transmitido para a SEFAZ. Reenviar poderia autorizar
    // duas notas para a mesma venda; fica sem decisão até a consulta por chave.
    const { request } = await stuckDocument();

    const provider = new ScriptedProvider(AUTHORIZED, IN_FLIGHT);
    const result = await fiscalDocumentStatus(context, request, provider, later());

    expect(result.status).toBe("processing");
    expect(provider.authorizeCalls).toBe(0);
  });

  it("não disputa com a chamada original ainda em voo", async () => {
    const { request } = await stuckDocument();

    const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);
    // `now` real: o documento acabou de ser reservado.
    const result = await fiscalDocumentStatus(context, request, provider);

    expect(result.status).toBe("processing");
    expect(provider.authorizeCalls).toBe(0);
  });

  it("produção bloqueada recusa ANTES de consumir número", async () => {
    // A trava de homologação vive no serviço fiscal, chamado só depois da
    // reserva. Sem a trava da nuvem, cada venda queimaria um número real da
    // série, e cada buraco exigiria inutilização formal na SEFAZ.
    delete process.env.FISCAL_PRODUCTION_ENABLED;
    await admin`UPDATE fiscal_configurations SET environment='production'
                 WHERE tenant_id=${tenant}`;
    try {
      const before = await nextNumber();
      const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);

      const error = await issueFiscalDocument(
        context, randomUUID(), await paidOrder(), provider,
      ).catch((caught: unknown) => caught);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(409);
      expect(provider.authorizeCalls).toBe(0);
      expect(await nextNumber()).toBe(before);
    } finally {
      await admin`UPDATE fiscal_configurations SET environment='homologation'
                   WHERE tenant_id=${tenant}`;
    }
  });

  it("sem serviço fiscal configurado, devolve 503 sem reservar número", async () => {
    // Antes as variáveis fiscais eram obrigatórias no processo inteiro, e o
    // deploy sem o serviço fiscal nunca ficava saudável. Agora a ausência é um
    // estado: a sincronização segue, e a emissão responde com clareza.
    const saved = {
      url: process.env.FISCAL_SERVICE_URL,
      token: process.env.FISCAL_SERVICE_TOKEN,
    };
    delete process.env.FISCAL_SERVICE_URL;
    delete process.env.FISCAL_SERVICE_TOKEN;
    try {
      const before = await nextNumber();

      const error = await issueFiscalDocument(context, randomUUID(), await paidOrder())
        .catch((caught: unknown) => caught);

      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(503);
      expect(await nextNumber()).toBe(before);
    } finally {
      if (saved.url) process.env.FISCAL_SERVICE_URL = saved.url;
      if (saved.token) process.env.FISCAL_SERVICE_TOKEN = saved.token;
    }
  });
  it("venda com desconto de 100% não emite nota e não consome número", async () => {
    // Uma NFC-e de R$ 0,00 não tem o que tributar e seria rejeitada depois de
    // já ter consumido um número da série — que então exigiria inutilização.
    const before = await nextNumber();
    const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);

    const result = await issueFiscalDocument(
      context, randomUUID(), await orderWithTotal(700, 0), provider,
    );

    expect(result.document.status).toBe("not_required");
    expect(result.created).toBe(false);
    expect(provider.authorizeCalls).toBe(0);
    expect(await nextNumber()).toBe(before);
    const [docs] = await admin<{ total: string }[]>`
      SELECT count(*) AS total FROM fiscal_documents WHERE tenant_id=${tenant}
        AND order_id IN (SELECT id FROM orders WHERE tenant_id=${tenant} AND total_cents=0)`;
    expect(Number(docs!.total)).toBe(0);
  });

  it("produto de preço zero também não emite", async () => {
    const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);

    const result = await issueFiscalDocument(
      context, randomUUID(), await orderWithTotal(0, 0), provider,
    );

    expect(result.document.status).toBe("not_required");
    expect(provider.authorizeCalls).toBe(0);
  });

  it("desconto parcial continua emitindo, pelo valor com desconto", async () => {
    // A regra é sobre o total ZERO, não sobre ter desconto: 99% de desconto
    // ainda é uma venda com valor, e ainda precisa de nota.
    const provider = new ScriptedProvider(AUTHORIZED, NOT_FOUND);

    const result = await issueFiscalDocument(
      context, randomUUID(), await orderWithTotal(700, 7), provider,
    );

    expect(result.document.status).toBe("authorized");
    expect(provider.authorizeCalls).toBe(1);
  });

  it("cortesia não precisa de nota nem com o serviço fiscal desligado", async () => {
    // A ordem das checagens importa: sem ela, a cortesia receberia 503
    // "fiscal não configurado", e o caixa mostraria um erro onde não há nada
    // de errado.
    const saved = {
      url: process.env.FISCAL_SERVICE_URL,
      token: process.env.FISCAL_SERVICE_TOKEN,
    };
    delete process.env.FISCAL_SERVICE_URL;
    delete process.env.FISCAL_SERVICE_TOKEN;
    try {
      const result = await issueFiscalDocument(
        context, randomUUID(), await orderWithTotal(700, 0),
      );
      expect(result.document.status).toBe("not_required");
    } finally {
      if (saved.url) process.env.FISCAL_SERVICE_URL = saved.url;
      if (saved.token) process.env.FISCAL_SERVICE_TOKEN = saved.token;
    }
  });

  it("total negativo é defeito, não cortesia", async () => {
    const error = await issueFiscalDocument(
      context, randomUUID(), await orderWithTotal(700, -100),
      new ScriptedProvider(AUTHORIZED, NOT_FOUND),
    ).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
  });
  // -- o emissor em C#: pagamento, QR Code v3 e o que ele calcula ---------------

  class CapturingProvider implements FiscalProvider {
    intents: FiscalIntent[] = [];
    async authorize(intent: FiscalIntent): Promise<FiscalProviderResult> {
      this.intents.push(intent);
      return AUTHORIZED;
    }
    async query(): Promise<FiscalProviderResult> {
      return NOT_FOUND;
    }
  }

  async function refusedBeforeReserving(orderId: string, expected: string): Promise<void> {
    const before = await nextNumber();
    const provider = new CapturingProvider();
    const error = await issueFiscalDocument(context, randomUUID(), orderId, provider)
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).message).toContain(expected);
    expect(await nextNumber()).toBe(before);
    expect(provider.intents).toHaveLength(0);
  }

  it("a intenção leva os pagamentos da venda, com o troco", async () => {
    const id = await paidOrder({ pay: false });
    await pay(id, 1000, 300);
    const provider = new CapturingProvider();

    await issueFiscalDocument(context, randomUUID(), id, provider);

    expect(provider.intents[0]!.payments).toEqual([{ method: "cash", amountCents: 1000, changeCents: 300 }]);
  });

  it("venda sem pagamento sincronizado é recusada antes de reservar", async () => {
    await refusedBeforeReserving(await paidOrder({ pay: false }), "sem pagamento");
  });

  it("pagamento que não fecha o total é recusado antes de reservar", async () => {
    const id = await paidOrder({ pay: false });
    await pay(id, 500);
    await refusedBeforeReserving(id, "não fecham o total");
  });

  it("forma de pagamento sem tPag é recusada antes de reservar", async () => {
    const id = await paidOrder({ pay: false });
    await pay(id, 700, 0, "voucher");
    await refusedBeforeReserving(id, "voucher");
  });

  it("tributação que o emissor não calcula é recusada antes de reservar", async () => {
    await admin`INSERT INTO fiscal_product_profiles
      (tenant_id,product_id,ncm,cfop,unit_code,origin,csosn,cst_pis,cst_cofins)
      VALUES (${tenant},'p101','21011200','5102','UN',0,'101','49','49')
      ON CONFLICT DO NOTHING`;
    const id = randomUUID();
    await admin`INSERT INTO orders
      (id,tenant_id,store_id,device_id,client_uuid,local_number,status,total_cents)
      VALUES (${id},${tenant},${store},${device},${randomUUID()},1,'paid',700)`;
    await admin`INSERT INTO order_items
      (id,tenant_id,order_id,client_uuid,product_id,product_name,quantity,
       unit_price_cents,total_cents,created_at)
      VALUES (${randomUUID()},${tenant},${id},${randomUUID()},'p101','Cafe com credito','1',700,700,now())`;
    await pay(id, 700);
    await refusedBeforeReserving(id, "CSOSN 101");
  });

  it("sem CSC a emissão segue: o QR Code v3 dispensa", async () => {
    await admin`UPDATE fiscal_configurations SET csc_ref=NULL, csc_id=NULL WHERE tenant_id=${tenant}`;
    try {
      const provider = new CapturingProvider();
      const result = await issueFiscalDocument(context, randomUUID(), await paidOrder(), provider);
      expect(issued(result.document).status).toBe("authorized");
      expect(provider.intents[0]!.cscRef).toBeUndefined();
      expect(provider.intents[0]!.cscId).toBeUndefined();
    } finally {
      await admin`UPDATE fiscal_configurations SET csc_ref='loja/csc', csc_id='1' WHERE tenant_id=${tenant}`;
    }
  });
});
