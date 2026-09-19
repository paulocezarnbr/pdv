/**
 * `POST /api/commands/issue` — o painel manda o terminal fazer alguma coisa.
 *
 * > ⚠️ Este é o **único** caminho pelo qual a nuvem comanda o PDV. Todo o
 * > resto do sistema flui na direção contrária. Quem comprometer este endpoint
 * > passa a conceder descontos e cancelar itens em todas as lojas ao mesmo
 * > tempo, sem pisar em nenhuma delas.
 *
 * A divisão de responsabilidade com o terminal
 * --------------------------------------------
 *
 * A nuvem **assina** e **entrega**. O terminal **confere** e **decide**. Não
 * há redundância aqui: as duas metades checam coisas diferentes, e a do
 * terminal é a que vale, porque é ela que continua valendo quando esta API é
 * a parte comprometida.
 *
 * * A nuvem confere quem é o emissor, se o perfil dele pode aquilo e se o
 *   terminal alvo pertence ao tenant. Isso evita **emitir** besteira.
 * * O terminal confere a assinatura, a validade, o teto do perfil lido da
 *   **réplica local** e o estado do pedido. Isso evita **obedecer** besteira.
 *
 * Se a segunda metade dependesse de a primeira ter feito o trabalho dela, um
 * painel comprometido teria poder total. Ver `remote/commands.py` no desktop.
 */

import { z } from "zod";

import { deviceSecret } from "@/lib/auth/device";
import { requirePanelUser } from "@/lib/auth/panel";
import { COMMAND_KINDS, canonicalPayload, signCommand } from "@/lib/crypto/commands";
import { toCommandOut, type CommandRow } from "@/lib/commands/serialize";
import { sql, withTenant } from "@/lib/db";
import { ApiError, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * Teto de comandos por operador na janela.
 *
 * Não é defesa contra o gerente distraído — é o que limita o estrago de uma
 * credencial vazada ao que dá para reverter numa manhã, em vez de uma noite
 * inteira de descontos em todas as lojas.
 */
const MAX_PER_OPERATOR = 60;
const RATE_WINDOW_MINUTES = 10;

const IssueSchema = z.object({
  device_id: z.string().min(1).max(64),
  kind: z.enum(COMMAND_KINDS),
  payload: z.record(z.unknown()).default({}),
  /**
   * Gerado pelo painel, como o `client_uuid` é gerado pelo celular do garçom.
   * Dois cliques no botão viram um comando só, e não dois descontos.
   */
  command_uuid: z.string().min(8).max(64),
});

export const POST = handler(async (request) => {
  // Quem chama é uma **pessoa** logada no painel, não um terminal. Por isso o
  // `issued_by_user_id` sai da sessão e nunca do corpo: aceitá-lo do corpo
  // deixaria trocar um campo para emitir em nome do dono.
  const user = await requirePanelUser(request);
  const body = await parseBody(request, IssueSchema);

  requireAuthorizer(user, body);
  await checkRateLimit(user.id, user.tenantId);

  const devices = await sql<{ id: string; store_id: string }[]>`
    SELECT id, store_id FROM devices
     WHERE id = ${body.device_id} AND tenant_id = ${user.tenantId}
       AND revoked_at IS NULL
  `;
  const device = devices[0];
  if (!device) {
    // 404 e não 403: o terminal não existe *neste tenant*. Distinguir os dois
    // casos contaria a um atacante quais device_id existem por aí.
    throw new ApiError(404, "Terminal não encontrado");
  }

  const issuedAt = new Date().toISOString();
  const secret = await deviceSecret(user.tenantId, device.id);
  const signature = signCommand(secret, {
    commandUuid: body.command_uuid,
    deviceId: device.id,
    kind: body.kind,
    payload: body.payload,
    issuedAt,
  });

  // `ON CONFLICT DO NOTHING` + leitura: dois cliques no painel, ou um retry do
  // navegador, devolvem o comando já emitido em vez de emitir outro. A
  // assinatura é determinística para o mesmo conteúdo, mas o `issued_at` não —
  // sem esta trava, o segundo clique produziria um comando novo, com uuid
  // igual e assinatura diferente.
  const stored = await withTenant(user.tenantId, async (tx) => {
    await tx`
      INSERT INTO remote_commands
        (command_uuid, tenant_id, store_id, device_id, kind, payload_json,
         issued_by_user_id, issued_by_name, issued_at, signature, status)
      VALUES (${body.command_uuid}, ${user.tenantId}, ${device.store_id},
              ${device.id}, ${body.kind}, ${canonicalPayload(body.payload)},
              ${user.id}, ${user.name}, ${issuedAt}, ${signature}, 'pending')
      ON CONFLICT (command_uuid) DO NOTHING
    `;
    return tx<CommandRow[]>`
      SELECT * FROM remote_commands
       WHERE command_uuid = ${body.command_uuid} AND tenant_id = ${user.tenantId}
    `;
  });

  const row = stored[0];
  if (!row) {
    // O uuid existe em outro tenant. Não se conta isso a quem perguntou.
    throw new ApiError(409, "Comando já registrado com outro conteúdo.");
  }

  console.info("[comando] emitido", {
    uuid: row.command_uuid,
    kind: row.kind,
    device: row.device_id,
    por: user.email,
  });

  return json(toCommandOut(row));
});

/**
 * Confere o perfil de quem emite, **antes** de assinar.
 *
 * Não substitui a conferência do terminal — duplica-a de propósito. Esta evita
 * emitir um comando nascido para ser recusado; a do terminal é a que continua
 * valendo quando esta API é a parte comprometida.
 */
function requireAuthorizer(
  user: { canAuthorize: boolean; maxDiscountPercent: number },
  body: { kind: string; payload: Record<string, unknown> },
): void {
  if (!user.canAuthorize) {
    throw new ApiError(403, "Seu perfil não autoriza operações remotas.");
  }

  if (body.kind !== "apply_discount") return;

  const percent = Number(body.payload["percent"]);
  if (!Number.isFinite(percent)) {
    throw new ApiError(400, "Percentual inválido");
  }
  if (percent <= 0 || percent > user.maxDiscountPercent) {
    throw new ApiError(
      403,
      `Você pode conceder até ${user.maxDiscountPercent}% — ` +
        `o pedido é de ${percent}%.`,
    );
  }
  if (!String(body.payload["reason"] ?? "").trim()) {
    throw new ApiError(400, "Informe o motivo");
  }
}

async function checkRateLimit(userId: string, tenantId: string): Promise<void> {
  const rows = await sql<{ total: string }[]>`
    SELECT COUNT(*) AS total FROM remote_commands
     WHERE tenant_id = ${tenantId} AND issued_by_user_id = ${userId}
       AND issued_at > now() - ${`${RATE_WINDOW_MINUTES} minutes`}::interval
  `;
  if (Number(rows[0]?.total ?? 0) >= MAX_PER_OPERATOR) {
    throw new ApiError(
      429,
      "Comandos demais em pouco tempo. Se não foi você, troque sua senha.",
    );
  }
}
