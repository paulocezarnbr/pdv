/**
 * `POST /api/devices/heartbeat` — o terminal conta como está.
 *
 * Fila pendente, quarentena, a idade do item mais antigo e o relógio. Vem a
 * cada ciclo de sincronização, **inclusive quando o envio falhou**: é
 * justamente aí que o dono precisa saber que as vendas estão presas.
 *
 * O desvio do relógio é calculado aqui, contra o relógio da nuvem. O terminal
 * informa a hora dele e nada mais — o caixa não é testemunha confiável da
 * própria hora, e um relógio atrasado é como uma venda de ontem vira de hoje.
 *
 * É telemetria: os números vêm do terminal e só servem para mostrar. Nada de
 * dinheiro é decidido a partir deles.
 */

import { z } from "zod";

import { requireDevice } from "@/lib/auth/device";
import { withTenant } from "@/lib/db";
import { ApiError, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const HeartbeatSchema = z.object({
  device_id: z.string().min(1).max(64),
  tenant_id: z.string().min(1).max(64),
  terminal_clock: z.string().datetime({ offset: true }),
  pending_items: z.number().int().min(0).max(10_000_000),
  quarantined_items: z.number().int().min(0).max(10_000_000),
  oldest_pending_at: z.string().datetime({ offset: true }).nullable(),
  last_quarantine_reason: z.string().max(2000).nullable(),
});

/** Teto do desvio guardado: um relógio em 1970 não pode estourar a coluna. */
const MAX_DRIFT_MS = 10 * 365 * 24 * 3600 * 1000;

export const POST = handler(async (request) => {
  const device = await requireDevice(request);
  const body = await parseBody(request, HeartbeatSchema);

  if (body.tenant_id !== device.tenantId || body.device_id !== device.deviceId) {
    throw new ApiError(403, "Identidade do terminal não confere");
  }

  const received = Date.now();
  const drift = Math.max(
    -MAX_DRIFT_MS,
    Math.min(MAX_DRIFT_MS, Date.parse(body.terminal_clock) - received),
  );

  await withTenant(device.tenantId, (tx) => tx`
    INSERT INTO device_telemetry
      (device_id, tenant_id, store_id, reported_at, terminal_clock, clock_drift_ms,
       pending_items, quarantined_items, oldest_pending_at, last_quarantine_reason)
    VALUES
      (${device.deviceId}, ${device.tenantId}, ${device.storeId}, to_timestamp(${received / 1000}),
       ${body.terminal_clock}, ${drift}, ${body.pending_items}, ${body.quarantined_items},
       ${body.oldest_pending_at}, ${body.last_quarantine_reason?.slice(0, 300) ?? null})
    ON CONFLICT (device_id) DO UPDATE SET
      reported_at            = EXCLUDED.reported_at,
      terminal_clock         = EXCLUDED.terminal_clock,
      clock_drift_ms         = EXCLUDED.clock_drift_ms,
      pending_items          = EXCLUDED.pending_items,
      quarantined_items      = EXCLUDED.quarantined_items,
      oldest_pending_at      = EXCLUDED.oldest_pending_at,
      last_quarantine_reason = EXCLUDED.last_quarantine_reason
  `);

  // O desvio volta para o terminal registrar no log: quem abre o log de uma
  // loja com venda "de ontem" encontra a causa na primeira linha.
  return json({ clock_drift_ms: drift });
});
