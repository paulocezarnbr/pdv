/**
 * `POST /api/commands/results` — o terminal conta o que fez com cada comando.
 *
 * Responde **quais** foram aceitos, e não um "ok" genérico. O terminal só tira
 * da fila de relato o que a nuvem nomear; uma confirmação em bloco esconderia
 * uma gravação parcial, e o painel ficaria mostrando `pendente` num comando já
 * aplicado — que é o estado em que alguém reemite o desconto na mão.
 */

import { z } from "zod";

import { requireDevice } from "@/lib/auth/device";
import { withTenant } from "@/lib/db";
import { ApiError, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const ResultSchema = z.object({
  command_uuid: z.string().min(8).max(64),
  status: z.enum(["applied", "refused"]),
  message: z.string().max(500).default(""),
  settled_at: z.string().max(64).default(""),
});

const ResultsSchema = z.object({
  tenant_id: z.string().min(1).max(64),
  store_id: z.string().min(1).max(64),
  device_id: z.string().min(1).max(64),
  results: z.array(ResultSchema).max(200),
});

export const POST = handler(async (request) => {
  const device = await requireDevice(request);
  const body = await parseBody(request, ResultsSchema);

  if (body.device_id !== device.deviceId || body.tenant_id !== device.tenantId) {
    throw new ApiError(403, "Identidade do terminal não confere");
  }

  const accepted: string[] = [];

  await withTenant(device.tenantId, async (tx) => {
    for (const result of body.results) {
      // `status = 'pending'` na cláusula: o terminal é a autoridade sobre o que
      // aconteceu, mas só na **primeira** vez que conta. Reescrever um
      // resultado já gravado deixaria um terminal comprometido apagar o
      // registro de um cancelamento que ele mesmo aplicou.
      await tx`
        UPDATE remote_commands
           SET status = ${result.status},
               result_message = ${result.message},
               settled_at = ${result.settled_at || new Date().toISOString()},
               reported_at = now()
         WHERE command_uuid = ${result.command_uuid}
           AND tenant_id = ${device.tenantId}
           AND device_id = ${device.deviceId}
           AND status = 'pending'
      `;

      // Zero linhas afetadas significa que o resultado já tinha chegado antes.
      // Isso é sucesso, não erro: o relato anterior chegou e só a resposta se
      // perdeu. Recusar aqui faria o terminal reavisar para sempre.
      accepted.push(result.command_uuid);
    }
  });

  return json({ accepted });
});
