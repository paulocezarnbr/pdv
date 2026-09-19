/**
 * `GET /api/commands/pending` — o terminal busca o que o painel mandou.
 *
 * Entrega não consome
 * -------------------
 *
 * Esta rota devolve o mesmo comando quantas vezes for preciso, até o terminal
 * relatar o que fez com ele. Consumir na entrega perderia o comando de vez se
 * o terminal morresse entre receber e gravar na inbox — e perder em silêncio é
 * pior que entregar duas vezes, porque a segunda entrega colide no
 * `command_uuid` do terminal e vira no-op.
 */

import { requireDevice } from "@/lib/auth/device";
import { toCommandOut, type CommandRow } from "@/lib/commands/serialize";
import { sql } from "@/lib/db";
import { handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Janela de validade, espelhando `MAX_COMMAND_AGE` do terminal. Emitir ou
 * entregar com prazo maior do que o terminal aceita só produziria comando
 * nascido morto — e a recusa gastaria um evento de auditoria com severidade
 * que não é a dele.
 */
const MAX_AGE_HOURS = 12;

export const GET = handler(async (request) => {
  const device = await requireDevice(request);
  const params = new URL(request.url).searchParams;

  const limit = Math.min(
    Math.max(Number.parseInt(params.get("limit") ?? "50", 10) || 50, 1),
    200,
  );

  // O `device_id` vem do **token**, nunca da query: aceitá-lo do cliente
  // deixaria um terminal comprometido ler os comandos endereçados a outro — e
  // um comando é assinado, então lê-lo é o primeiro passo para replayá-lo.
  const rows = await sql<CommandRow[]>`
    SELECT * FROM remote_commands
     WHERE tenant_id = ${device.tenantId}
       AND device_id = ${device.deviceId}
       AND status = 'pending'
       AND issued_at > now() - ${`${MAX_AGE_HOURS} hours`}::interval
     ORDER BY issued_at
     LIMIT ${limit}
  `;

  if (rows.length > 0) {
    // A entrega é registrada mas **não** consome: ver o cabeçalho.
    const uuids = rows.map((row) => row.command_uuid);
    await sql`
      UPDATE remote_commands
         SET delivered_at = COALESCE(delivered_at, now()),
             delivery_count = delivery_count + 1
       WHERE command_uuid = ANY(${uuids})
    `;
  }

  return json({ commands: rows.map(toCommandOut) });
});
