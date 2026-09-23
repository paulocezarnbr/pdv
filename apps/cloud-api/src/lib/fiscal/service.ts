import type { DeviceContext } from "@/lib/auth/device";
import { withTenant, type Tx } from "@/lib/db";
import { env } from "@/lib/env";
import { ApiError } from "@/lib/http";
import {
  FiscalProviderUnavailable,
  PythonFiscalProvider,
  type FiscalIntent,
  type FiscalProvider,
  type FiscalProviderResult,
} from "@/lib/fiscal/provider";

export interface FiscalDocumentOut {
  document_id: string;
  request_uuid: string;
  order_id: string;
  model: number;
  series: number;
  number: number;
  status: string;
  access_key: string | null;
  protocol: string | null;
  provider_code: string | null;
  provider_reason: string | null;
  issued_at: string;
  authorized_at: string | null;
}

interface DocumentRow {
  id: string;
  tenant_id: string;
  store_id: string;
  device_id: string;
  order_id: string;
  request_uuid: string;
  model: number;
  series: number;
  number: string;
  status: string;
  access_key: string | null;
  protocol: string | null;
  provider_code: string | null;
  provider_reason: string | null;
  issued_at: Date;
  authorized_at: Date | null;
}

interface ConfigRow {
  uf: string;
  environment: "homologation" | "production";
  certificate_ref: string | null;
  csc_ref: string | null;
  csc_id: string | null;
  cnpj: string | null;
  state_registration: string | null;
  tax_regime: number | null;
  legal_name: string | null;
  address_json: Record<string, unknown>;
  enabled: boolean;
}

interface ItemRow {
  product_id: string | null;
  product_name: string;
  quantity: string;
  unit_price_cents: string;
  total_cents: string;
  ncm: string | null;
  cfop: string | null;
  cest: string | null;
  unit_code: string | null;
  origin: number | null;
  csosn: string | null;
  cst_icms: string | null;
  cst_pis: string | null;
  cst_cofins: string | null;
}

/**
 * Reserva e autoriza uma NFC-e normal.
 *
 * `created=false` é a trava de idempotência: só o processo que inseriu a
 * linha chama a SEFAZ. Repetições devolvem a linha existente. Se a chamada
 * ficar ambígua, o estado é `unknown`; jamais se autoriza outra nota local.
 */
export async function issueFiscalDocument(
  device: DeviceContext,
  requestUuid: string,
  orderId: string,
  provider?: FiscalProvider,
): Promise<{ document: FiscalDocumentOut; created: boolean }> {
  // O provedor é resolvido ANTES da reserva. Resolvido como parâmetro
  // padrão, ele era construído antes de tudo e, sem as variáveis fiscais,
  // estourava um erro genérico (500) que o terminal trata como resultado
  // ambíguo. Aqui a ausência vira um 503 claro, e nenhum número é consumido.
  const fiscal = provider ?? defaultProvider();

  const prepared = await reserve(device, requestUuid, orderId);
  if (!prepared.created) return { document: out(prepared.row), created: false };
  return { document: out(await transmit(device, prepared.row, prepared.intent, fiscal)), created: true };
}

/**
 * Chama o provedor e grava o resultado. Separado de `issueFiscalDocument`
 * porque a reconciliação também precisa dele: um documento que ficou
 * `processing` porque o processo caiu entre reservar e transmitir é
 * retransmitido por aqui, com o MESMO número e o MESMO `request_uuid`.
 */
async function transmit(
  device: DeviceContext,
  row: DocumentRow,
  intent: FiscalIntent,
  provider: FiscalProvider,
): Promise<DocumentRow> {

  let result: FiscalProviderResult;
  try {
    result = await provider.authorize(intent);
  } catch (error) {
    if (!(error instanceof FiscalProviderUnavailable)) throw error;
    result = {
      status: "unknown",
      code: "TRANSPORT_UNKNOWN",
      reason: "A autorização pode ter chegado à SEFAZ; consulte pela chave antes de reenviar.",
    };
  }

  return settle(device.tenantId, row.id, result);
}

function defaultProvider(): FiscalProvider {
  if (!env.fiscalConfigured) {
    throw new ApiError(
      503,
      "Emissão fiscal não configurada nesta retaguarda. Nenhum número foi reservado.",
    );
  }
  return new PythonFiscalProvider();
}

/**
 * Quanto um documento ainda não concluído espera antes de ser retransmitido
 * na reconciliação.
 *
 * A retransmissão é segura em qualquer momento — o serviço fiscal reivindica o
 * `request_uuid` atomicamente e executa o motor uma vez só. A espera existe
 * para não disputar com a própria chamada original ainda em voo, o que
 * dobraria a carga e sujaria o log com uma corrida que não é defeito.
 */
const RETRANSMIT_AFTER_MS = 10_000;

export async function fiscalDocumentStatus(
  device: DeviceContext,
  requestUuid: string,
  provider?: FiscalProvider,
  now: Date = new Date(),
): Promise<FiscalDocumentOut> {
  const rows = await withTenant(device.tenantId, (tx) => tx<DocumentRow[]>`
    SELECT * FROM fiscal_documents
     WHERE tenant_id=${device.tenantId} AND store_id=${device.storeId}
       AND device_id=${device.deviceId} AND request_uuid=${requestUuid}::uuid
  `);
  if (!rows[0]) throw new ApiError(404, "Solicitação fiscal não encontrada.");
  const row = rows[0];
  if (row.status !== "unknown" && row.status !== "processing") return out(row);
  if (!provider && !env.fiscalConfigured) return out(row);
  const fiscal = provider ?? new PythonFiscalProvider();

  // O request anterior pode ter sido autorizado e perdido apenas a resposta.
  // Consulta o ledger idempotente do serviço antes de qualquer novo envio.
  let result: FiscalProviderResult;
  try {
    result = await fiscal.query(requestUuid);
  } catch (error) {
    if (error instanceof FiscalProviderUnavailable) return out(row);
    throw error;
  }
  if (result.status !== "unknown") return out(await settle(device.tenantId, row.id, result));

  // Os dois `unknown` têm consequências opostas, e é o serviço fiscal quem
  // sabe qual é qual:
  //
  // * `NOT_FOUND` — a chamada nunca chegou lá. O motor não rodou e a SEFAZ não
  //   viu nada. É o documento cujo processo caiu entre reservar o número e
  //   transmitir, que antes ficava `processing` para sempre. Retransmite com o
  //   MESMO número e o MESMO `request_uuid`.
  // * qualquer outro código (`IN_FLIGHT`, `ENGINE_FAILURE`) — o motor pode ter
  //   transmitido. Retransmitir poderia autorizar duas notas para uma venda;
  //   fica `unknown` até uma consulta por chave na SEFAZ.
  if (result.code !== "NOT_FOUND") return out(row);
  if (now.getTime() - row.issued_at.getTime() < RETRANSMIT_AFTER_MS) return out(row);

  let intent: FiscalIntent;
  try {
    intent = await withTenant(device.tenantId, async (tx) => {
      const { order, config, items } = await readEmissionInputs(tx, device, row.order_id);
      return makeIntent(device, row, order.total_cents, config, items);
    });
  } catch (error) {
    // Configuração desligada, produção bloqueada ou item sem perfil desde a
    // reserva: não há o que retransmitir agora, e o documento continua como
    // estava. Recusar a consulta com 409 faria o terminal ler um erro onde só
    // existe "ainda sem decisão".
    if (error instanceof ApiError) return out(row);
    throw error;
  }

  console.info("[fiscal] retransmitindo documento que nunca chegou ao serviço", {
    document: row.id, series: row.series, number: row.number,
  });
  return out(await transmit(device, row, intent, fiscal));
}

async function reserve(
  device: DeviceContext,
  requestUuid: string,
  orderId: string,
): Promise<{ row: DocumentRow; intent: FiscalIntent; created: boolean }> {
  return withTenant(device.tenantId, async (tx) => {
    const existing = await tx<DocumentRow[]>`
      SELECT * FROM fiscal_documents
       WHERE tenant_id=${device.tenantId} AND request_uuid=${requestUuid}::uuid
    `;
    if (existing[0]) {
      if (existing[0].order_id !== orderId || existing[0].device_id !== device.deviceId) {
        throw new ApiError(409, "Chave de idempotência já pertence a outra venda.");
      }
      return { row: existing[0], intent: {} as FiscalIntent, created: false };
    }

    const { order, config, items } = await readEmissionInputs(tx, device, orderId);

    const seriesRows = await tx<{ id: string; series: number; next_number: string }[]>`
      SELECT id, series, next_number FROM fiscal_series
       WHERE tenant_id=${device.tenantId} AND store_id=${device.storeId}
         AND model=65 AND purpose='normal' AND device_id IS NULL
       FOR UPDATE
    `;
    const series = seriesRows[0];
    if (!series) throw new ApiError(409, "Série normal NFC-e não configurada na nuvem.");

    const inserted = await tx<DocumentRow[]>`
      INSERT INTO fiscal_documents
        (tenant_id,store_id,device_id,order_id,request_uuid,model,series,number,status)
      VALUES (${device.tenantId},${device.storeId},${device.deviceId},${orderId}::uuid,
              ${requestUuid}::uuid,65,${series.series},${series.next_number},'processing')
      ON CONFLICT (tenant_id,request_uuid) DO NOTHING
      RETURNING *
    `;
    if (!inserted[0]) {
      const raced = await tx<DocumentRow[]>`
        SELECT * FROM fiscal_documents
         WHERE tenant_id=${device.tenantId} AND request_uuid=${requestUuid}::uuid
      `;
      const row = raced[0];
      if (!row || row.order_id !== orderId || row.device_id !== device.deviceId) {
        throw new ApiError(409, "Chave de idempotência já pertence a outra venda.");
      }
      return { row, intent: {} as FiscalIntent, created: false };
    }

    await tx`UPDATE fiscal_series SET next_number=next_number+1,updated_at=now()
              WHERE id=${series.id}`;
    await tx`INSERT INTO fiscal_events
      (tenant_id,fiscal_document_id,event_type,payload_json)
      VALUES (${device.tenantId},${inserted[0].id},'reserved',
              ${{ source: "cloud", atomic: true } as never})`;

    return {
      row: inserted[0],
      created: true,
      intent: makeIntent(device, inserted[0], order.total_cents, config, items),
    };
  });
}

/**
 * Pedido, configuração e itens de uma emissão, já validados.
 *
 * Uma função só para a reserva e para a reconciliação, de propósito: a
 * retransmissão de um documento preso passa pelas MESMAS checagens da
 * primeira emissão — inclusive a trava de produção. Duas cópias da validação
 * divergiriam, e a que diverge seria a do caminho raro, que é o menos testado.
 */
async function readEmissionInputs(
  tx: Tx,
  device: DeviceContext,
  orderId: string,
): Promise<{ order: { total_cents: string }; config: ConfigRow; items: ItemRow[] }> {
  const orders = await tx<{ id: string; status: string; total_cents: string }[]>`
    SELECT id, status, total_cents FROM orders
     WHERE id=${orderId}::uuid AND tenant_id=${device.tenantId}
       AND store_id=${device.storeId} AND device_id=${device.deviceId}
     FOR SHARE
  `;
  const order = orders[0];
  if (!order) throw new ApiError(404, "Venda paga não encontrada neste terminal.");
  if (order.status !== "paid") throw new ApiError(409, "Somente venda paga emite NFC-e.");

  const configurations = await tx<ConfigRow[]>`
    SELECT * FROM fiscal_configurations
     WHERE tenant_id=${device.tenantId} AND store_id=${device.storeId}
     FOR SHARE
  `;
  const config = configurations[0];
  validateConfiguration(config);

  const items = await tx<ItemRow[]>`
    SELECT oi.product_id, oi.product_name, oi.quantity, oi.unit_price_cents,
           oi.total_cents, fp.ncm, fp.cfop, fp.cest, fp.unit_code, fp.origin,
           fp.csosn, fp.cst_icms, fp.cst_pis, fp.cst_cofins
      FROM order_items oi
      LEFT JOIN fiscal_product_profiles fp
        ON fp.tenant_id=oi.tenant_id AND fp.product_id=oi.product_id
     WHERE oi.tenant_id=${device.tenantId} AND oi.order_id=${orderId}::uuid
       AND oi.canceled_at IS NULL
     ORDER BY oi.created_at, oi.id
  `;
  validateItems(items);

  return { order, config, items };
}

async function settle(
  tenantId: string,
  documentId: string,
  result: FiscalProviderResult,
): Promise<DocumentRow> {
  return withTenant(tenantId, async (tx) => {
    const rows = await tx<DocumentRow[]>`
      UPDATE fiscal_documents SET
        status=${result.status}, access_key=${result.accessKey ?? null},
        protocol=${result.protocol ?? null}, xml_content=${result.processedXml ?? null},
        provider_code=${result.code}, provider_reason=${result.reason},
        authorized_at=${result.status === "authorized" ? new Date() : null}, updated_at=now()
       WHERE id=${documentId} AND tenant_id=${tenantId}
         AND status IN ('processing','unknown')
       RETURNING *
    `;
    if (!rows[0]) {
      const current = await tx<DocumentRow[]>`
        SELECT * FROM fiscal_documents WHERE id=${documentId} AND tenant_id=${tenantId}
      `;
      if (!current[0]) throw new ApiError(404, "Documento fiscal não encontrado.");
      return current[0];
    }
    await tx`INSERT INTO fiscal_events
      (tenant_id,fiscal_document_id,event_type,payload_json)
      VALUES (${tenantId},${documentId},${result.status},
              ${{ code: result.code, reason: result.reason } as never})`;
    return rows[0];
  });
}

function validateConfiguration(config: ConfigRow | undefined): asserts config is ConfigRow {
  if (!config?.enabled) throw new ApiError(409, "Emissão fiscal não habilitada para esta loja.");
  // Antes da reserva, e esse é o ponto: a trava de homologação do motor vive
  // no serviço fiscal, que só é chamado DEPOIS de o número ser consumido.
  // Sem esta checagem, uma loja em `production` queimaria um número real por
  // venda, o motor travado rejeitaria, e cada buraco exigiria inutilização
  // formal na SEFAZ. Ver `env.fiscalProductionEnabled`.
  if (config.environment === "production" && !env.fiscalProductionEnabled) {
    throw new ApiError(
      409,
      "Emissão em produção bloqueada: o motor fiscal ainda não foi homologado. " +
        "Nenhum número foi consumido.",
    );
  }
  const required = [config.certificate_ref, config.csc_ref, config.csc_id, config.cnpj,
    config.state_registration, config.tax_regime, config.legal_name];
  if (required.some((value) => value === null || value === "")) {
    throw new ApiError(409, "Configuração fiscal incompleta; nenhum valor será inventado.");
  }
}

function validateItems(items: ItemRow[]): void {
  if (!items.length) throw new ApiError(409, "Venda sem itens fiscais ativos.");
  const invalid = items.find((item) => !item.product_id || !item.ncm || !item.cfop ||
    !item.unit_code || item.origin === null || !item.cst_pis || !item.cst_cofins ||
    (!item.csosn && !item.cst_icms));
  if (invalid) {
    throw new ApiError(409, `Produto sem perfil tributário completo: ${invalid.product_name}.`);
  }
}

function makeIntent(
  device: DeviceContext,
  document: DocumentRow,
  totalCents: string,
  config: ConfigRow,
  items: ItemRow[],
): FiscalIntent {
  return {
    documentId: document.id, requestUuid: document.request_uuid, orderId: document.order_id,
    tenantId: device.tenantId, storeId: device.storeId, deviceId: device.deviceId,
    model: 65, series: document.series, number: Number(document.number),
    environment: config.environment, certificateRef: config.certificate_ref!,
    cscRef: config.csc_ref!, cscId: config.csc_id!,
    issuer: { uf: config.uf, cnpj: config.cnpj!, stateRegistration: config.state_registration!,
      taxRegime: config.tax_regime!, legalName: config.legal_name!, address: config.address_json },
    totalCents: Number(totalCents),
    items: items.map((item) => ({
      productId: item.product_id!, name: item.product_name, quantity: item.quantity,
      unitPriceCents: Number(item.unit_price_cents), totalCents: Number(item.total_cents),
      ncm: item.ncm!, cfop: item.cfop!, cest: item.cest ?? undefined,
      unitCode: item.unit_code!, origin: item.origin!, csosn: item.csosn ?? undefined,
      cstIcms: item.cst_icms ?? undefined, cstPis: item.cst_pis!, cstCofins: item.cst_cofins!,
    })),
  };
}

function out(row: DocumentRow): FiscalDocumentOut {
  return {
    document_id: row.id, request_uuid: row.request_uuid, order_id: row.order_id,
    model: row.model, series: row.series, number: Number(row.number), status: row.status,
    access_key: row.access_key, protocol: row.protocol, provider_code: row.provider_code,
    provider_reason: row.provider_reason, issued_at: row.issued_at.toISOString(),
    authorized_at: row.authorized_at?.toISOString() ?? null,
  };
}
