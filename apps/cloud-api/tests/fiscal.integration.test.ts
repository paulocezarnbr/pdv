import { randomUUID } from "node:crypto";

import postgres from "postgres";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { DeviceContext } from "../src/lib/auth/device.ts";
import {
  FiscalProviderUnavailable,
  type FiscalIntent,
  type FiscalProviderResult,
} from "../src/lib/fiscal/provider.ts";
import { fiscalDocumentStatus, issueFiscalDocument } from "../src/lib/fiscal/service.ts";

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const describeDb = ADMIN_URL ? describe : describe.skip;

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
    }
    context = { tenantId: tenant, storeId: store, deviceId: device, storeName: "Loja" };
  });

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
      await admin`DELETE FROM order_items WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM orders WHERE tenant_id=${tenant}`;
      await admin`DELETE FROM tenants WHERE id=${tenant}`;
    } finally {
      await admin`SET session_replication_role = origin`;
    }
    await admin.end({ timeout: 5 });
  });

  it("repete a mesma solicitação sem chamar o provedor ou consumir número", async () => {
    const provider = new FakeProvider({ status: "authorized", code: "100",
      reason: "Autorizado", accessKey: "3".repeat(44), protocol: "123" });
    const request = randomUUID();
    const first = await issueFiscalDocument(context, request, order, provider);
    const repeated = await issueFiscalDocument(context, request, order, provider);
    expect(first.document.number).toBe(1);
    expect(repeated.document.document_id).toBe(first.document.document_id);
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
});
