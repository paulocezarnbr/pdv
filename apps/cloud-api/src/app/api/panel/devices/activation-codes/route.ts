/**
 * `POST /api/panel/devices/activation-codes` — o painel gera o código que
 * ativa um caixa novo.
 *
 * Sem esta rota, a ativação só existia nos testes: o código era inserido
 * direto no banco, e quem instalava o PDV numa loja de verdade não tinha de
 * onde tirá-lo.
 *
 * O código volta **uma vez**, nesta resposta. O banco guarda só o hash, e a
 * auditoria registra quem gerou, para qual loja e com que nome — nunca o
 * código.
 */

import { randomUUID } from "node:crypto";

import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { CODE_TTL_MINUTES, codeHash, newActivationCode } from "@/lib/devices/activation-code";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const IssueSchema = z.object({
  store_id: z.string().uuid(),
  label: z.string().trim().min(1).max(60),
});

export const POST = handler(async (request) => {
  const actor = await requirePanelUser(request);
  // O código entrega um token que vende em nome da loja: quem só olha o
  // painel não o gera.
  if (actor.role !== "owner" && actor.role !== "manager") {
    throw new ApiError(403, "Somente o dono ou o gerente ativa um terminal.");
  }
  const body = await parseBody(request, IssueSchema);
  const code = newActivationCode();
  const deviceId = randomUUID();

  const issued = await withTenant(actor.tenantId, async (tx) => {
    const stores = await tx<{ name: string }[]>`
      SELECT name FROM stores WHERE id = ${body.store_id} AND tenant_id = ${actor.tenantId}
    `;
    const store = stores[0];
    if (!store) throw new ApiError(404, "Loja não encontrada.");

    const rows = await tx<{ expires_at: string }[]>`
      INSERT INTO device_activation_codes (code_hash, tenant_id, store_id, device_id, label)
      VALUES (${codeHash(code)}, ${actor.tenantId}, ${body.store_id}, ${deviceId}, ${body.label})
      RETURNING (created_at + ${`${CODE_TTL_MINUTES} minutes`}::interval)::text AS expires_at
    `;
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${actor.tenantId}, ${actor.id}, 'activation_code_issued',
              ${tx.json({ store_id: body.store_id, device_id: deviceId, label: body.label } as never)},
              ${clientIp(request)})
    `;
    return { store_name: store.name, expires_at: rows[0]!.expires_at };
  });

  return json(
    {
      code,
      label: body.label,
      device_id: deviceId,
      store_name: issued.store_name,
      expires_at: issued.expires_at,
      ttl_minutes: CODE_TTL_MINUTES,
    },
    { status: 201, headers: { "Cache-Control": "no-store" } },
  );
});
