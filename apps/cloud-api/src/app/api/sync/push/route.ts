/**
 * `POST /api/sync/push` — o endpoint mais sensível do sistema.
 *
 * É por ele que entra o faturamento de todas as lojas. Três controles não
 * negociáveis:
 *
 * * **Tenant vem do token, nunca do corpo.** Se o `tenant_id` do payload fosse
 *   aceito, um terminal comprometido gravaria vendas no tenant de outro
 *   restaurante — ou leria o dele.
 * * **Uma transação por lote.** Aplicação parcial corromperia a relação entre
 *   venda, estoque e auditoria.
 * * **`Idempotency-Key` obrigatório.** É o que permite ao cliente reenviar com
 *   segurança quando não sabe se a primeira tentativa chegou — e reenviar sob
 *   dúvida é o comportamento NORMAL do outbox, não a exceção.
 */

import { z } from "zod";

import { requireDevice, deviceSecret } from "@/lib/auth/device";
import { sql, withTenant } from "@/lib/db";
import { ApiError, handler, json, parseBody } from "@/lib/http";
import { SyncMerger, type ItemResult, type SyncItem } from "@/lib/sync/merge";

export const dynamic = "force-dynamic";

const ItemSchema = z.object({
  entity_table: z.string().min(1).max(64),
  entity_id: z.string().min(1).max(64),
  client_uuid: z.string().min(1).max(64),
  operation: z.enum(["insert", "update", "delete"]),
  payload: z.record(z.unknown()),
});

const PushSchema = z.object({
  device_id: z.string().min(1).max(64),
  tenant_id: z.string().min(1).max(64),
  store_id: z.string().min(1).max(64),
  // Teto de lote: protege contra um terminal (ou um atacante) despejar um
  // payload gigante e segurar uma conexão de banco indefinidamente.
  items: z.array(ItemSchema).max(500),
});

export const POST = handler(async (request) => {
  const device = await requireDevice(request);

  const idempotencyKey = request.headers.get("idempotency-key")?.trim();
  if (!idempotencyKey) {
    throw new ApiError(400, "Idempotency-Key é obrigatório");
  }

  const body = await parseBody(request, PushSchema);

  // Divergência entre token e corpo é sinal de terminal clonado ou de tentativa
  // de escrita cruzada entre tenants. Corrigir em silêncio esconderia o sinal.
  if (body.tenant_id !== device.tenantId || body.device_id !== device.deviceId) {
    console.warn("[sync] identidade divergente", {
      token: { tenant: device.tenantId, device: device.deviceId },
      corpo: { tenant: body.tenant_id, device: body.device_id },
    });
    throw new ApiError(403, "Identidade do terminal não confere");
  }

  // O lote repetido devolve a MESMA resposta, sem reprocessar. Não é só
  // economia: o cliente casa cada item pelo `client_uuid`, e uma segunda
  // execução devolveria `duplicate` onde a primeira devolveu `applied` —
  // fazendo o terminal achar que a venda não entrou por este lote.
  const cached = await sql<{ response_json: unknown }[]>`
    SELECT response_json FROM sync_batches
     WHERE tenant_id = ${device.tenantId}
       AND device_id = ${device.deviceId}
       AND idempotency_key = ${idempotencyKey}
  `;
  if (cached[0]) {
    return json(cached[0].response_json, { headers: { "X-Idempotent-Replay": "1" } });
  }

  if (body.items.length === 0) {
    return json({ results: [], applied: 0, duplicates: 0, rejected: 0 });
  }

  const secret = await deviceSecret(device.tenantId, device.deviceId);
  const merger = new SyncMerger({
    tenantId: device.tenantId,
    storeId: device.storeId,
    deviceId: device.deviceId,
    secret,
  });

  // UMA transação para o lote inteiro, declarando o tenant: o RLS é a segunda
  // barreira, para o caso de uma consulta nova esquecer o `WHERE tenant_id`.
  const results = await withTenant(device.tenantId, (tx) =>
    merger.apply(body.items as SyncItem[], tx),
  );

  const response = summarize(results);

  // Gravado FORA da transação do lote, e de propósito: se a gravação da
  // resposta falhasse dentro dela, o lote inteiro seria desfeito por causa de
  // um registro de conveniência. Perder a cópia só faz o reenvio reprocessar,
  // e reprocessar é seguro — é o que as quatro regras garantem.
  await sql`
    INSERT INTO sync_batches (tenant_id, device_id, idempotency_key, response_json)
    VALUES (${device.tenantId}, ${device.deviceId}, ${idempotencyKey},
            ${sql.json(response as never)})
    ON CONFLICT DO NOTHING
  `.catch((error: unknown) =>
    console.warn("[sync] não foi possível guardar a resposta do lote", error),
  );

  return json(response);
});

function summarize(results: ItemResult[]) {
  return {
    results,
    applied: results.filter((r) => r.status === "applied").length,
    duplicates: results.filter((r) => r.status === "duplicate").length,
    rejected: results.filter((r) => r.status === "rejected").length,
  };
}
