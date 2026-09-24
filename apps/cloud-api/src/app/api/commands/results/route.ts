/**
 * `POST /api/commands/results` — o terminal conta o que fez com cada comando.
 *
 * Responde **quais** foram aceitos, e não um "ok" genérico. O terminal só tira
 * da fila de relato o que a nuvem nomear; uma confirmação em bloco esconderia
 * uma gravação parcial, e o painel ficaria mostrando `pendente` num comando já
 * aplicado — que é o estado em que alguém reemite o desconto na mão.
 *
 * `awaiting` é o aviso de que um comando parou no caixa, esperando o aceite de
 * alguém presente (cancelar item que já foi para a cozinha). Não é resultado:
 * o comando continua `pending` e continua sendo entregue. Viaja em campo à
 * parte porque um terminal antigo não o manda, e uma nuvem antiga o ignora —
 * se fosse um terceiro status em `results`, a nuvem antiga recusaria o lote
 * inteiro e o desconto já aplicado ficaria "pendente" no painel.
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

const AwaitingSchema = z.object({
  command_uuid: z.string().min(8).max(64),
  message: z.string().max(500).default(""),
  requested_at: z.string().max(64).default(""),
});

const ResultsSchema = z.object({
  tenant_id: z.string().min(1).max(64),
  store_id: z.string().min(1).max(64),
  device_id: z.string().min(1).max(64),
  results: z.array(ResultSchema).max(200).default([]),
  awaiting: z.array(AwaitingSchema).max(200).default([]),
});

export const POST = handler(async (request) => {
  const device = await requireDevice(request);
  const body = await parseBody(request, ResultsSchema);

  if (body.device_id !== device.deviceId || body.tenant_id !== device.tenantId) {
    throw new ApiError(403, "Identidade do terminal não confere");
  }

  const accepted: string[] = [];

  await withTenant(device.tenantId, async (tx) => {
    // A espera primeiro. Se o mesmo comando vier nas duas listas (esperou e
    // foi decidido entre dois ciclos), o resultado grava depois e prevalece —
    // e a cláusula `status = 'pending'` impede a espera de voltar por cima.
    for (const notice of body.awaiting) {
      await tx`
        UPDATE remote_commands
           SET awaiting_confirmation_at = COALESCE(
                 awaiting_confirmation_at,
                 ${validInstant(notice.requested_at) ?? new Date().toISOString()}::timestamptz
               ),
               awaiting_message = ${notice.message}
         WHERE command_uuid = ${notice.command_uuid}
           AND tenant_id = ${device.tenantId}
           AND device_id = ${device.deviceId}
           AND status = 'pending'
      `;
      accepted.push(notice.command_uuid);
    }

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

/**
 * O instante que o terminal informou, se for um instante.
 *
 * O relógio do caixa não é confiável (invariante 8), mas aqui ele só diz desde
 * quando o comando espera, para o painel. Texto que não é data vira "agora" em
 * vez de derrubar o lote: o relato final dos outros comandos vale mais que a
 * precisão de um aviso.
 */
function validInstant(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}
