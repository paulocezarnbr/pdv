/**
 * `POST /api/devices/activate` — o pareamento entre o PDV e a retaguarda.
 *
 * Este é o **único** endpoint do sistema que aceita uma requisição sem token:
 * é justamente ele que entrega o token. Daí o cuidado desproporcional ao
 * tamanho do código.
 *
 * Controles
 * ---------
 *
 * * **Código de uso único e de vida curta.** É ditado por telefone para quem
 *   está no balcão; se vazar, precisa expirar antes de valer alguma coisa.
 * * **Consumo atômico.** O `UPDATE ... WHERE used_at IS NULL RETURNING`
 *   garante que duas requisições simultâneas com o mesmo código produzem
 *   exatamente uma ativação. Ler-e-depois-gravar abriria uma janela de corrida
 *   na qual dois terminais receberiam a mesma identidade.
 * * **Busca por hash.** O código é segredo de curta duração; um dump do banco
 *   não pode entregar códigos ativos.
 * * **Limite de tentativas por IP.** Sem isso, um código curto cai por força
 *   bruta dentro da janela de validade.
 *
 * O que este endpoint **não** faz
 * -------------------------------
 *
 * Não envia nem recebe o `device_secret` do ledger. Ele é gerado no terminal e
 * nunca sai de lá — ver `provisioning/secrets.py` no desktop. O que a nuvem
 * guarda é uma cópia recebida na ativação, usada só para **conferir** a cadeia
 * que o terminal envia; ela nunca volta para o terminal nem para o painel.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

import { z } from "zod";

import { sql, withTenant } from "@/lib/db";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

/** Validade do código a partir da geração no painel. */
const CODE_TTL_MINUTES = 15;

/**
 * Tentativas por IP na janela, antes de recusar. Com este teto a força bruta
 * de um código de alta entropia é inviável dentro dos 15 minutos de validade.
 */
const MAX_ATTEMPTS_PER_IP = 10;
const ATTEMPT_WINDOW_MINUTES = 15;

const ActivationSchema = z.object({
  activation_code: z.string().min(6).max(32),
  fingerprint: z
    .object({
      hostname: z.string().max(128).default(""),
      os: z.string().max(128).default(""),
      arch: z.string().max(64).default(""),
    })
    .default({ hostname: "", os: "", arch: "" }),
  /**
   * O segredo HMAC do ledger, gerado no terminal na primeira execução.
   *
   * Sobe UMA vez, na ativação, por HTTPS, e nunca mais trafega. A nuvem
   * precisa dele para recalcular a cadeia de auditoria que o terminal envia —
   * sem ele, "o servidor confere" viraria "o servidor acredita", e a única
   * garantia forte do sistema deixaria de existir.
   */
  device_secret_hex: z.string().regex(/^[0-9a-f]{32,128}$/i).optional(),
});

function codeHash(code: string): string {
  // SHA-256 sem sal basta aqui: o código é aleatório de alta entropia e vive
  // 15 minutos, então não há dicionário a proteger — diferente de uma senha
  // escolhida por gente.
  return createHash("sha256").update(code.trim().toUpperCase(), "utf8").digest("hex");
}

export const POST = handler(async (request) => {
  const body = await parseBody(request, ActivationSchema);
  const ip = clientIp(request);

  const attempts = await sql<{ total: string }[]>`
    SELECT COUNT(*) AS total FROM device_activation_attempts
     WHERE ip = ${ip}
       AND attempted_at > now() - ${`${ATTEMPT_WINDOW_MINUTES} minutes`}::interval
  `;
  if (Number(attempts[0]?.total ?? 0) >= MAX_ATTEMPTS_PER_IP) {
    throw new ApiError(
      429,
      "Muitas tentativas. Aguarde alguns minutos e gere um novo código.",
    );
  }

  await sql`INSERT INTO device_activation_attempts (ip) VALUES (${ip})`;

  // Consumo atômico: o WHERE carrega TODAS as condições de validade, e o
  // RETURNING só devolve linha se ESTA requisição foi a que consumiu. Duas
  // chamadas simultâneas com o mesmo código: uma recebe a linha, a outra
  // recebe nada e leva 410.
  const claimed = await sql<
    { tenant_id: string; store_id: string; device_id: string; label: string }[]
  >`
    UPDATE device_activation_codes
       SET used_at = now(), used_by_ip = ${ip}
     WHERE code_hash = ${codeHash(body.activation_code)}
       AND used_at IS NULL
       AND revoked_at IS NULL
       AND created_at > now() - ${`${CODE_TTL_MINUTES} minutes`}::interval
    RETURNING tenant_id, store_id, device_id, label
  `;

  const code = claimed[0];
  if (!code) {
    // Mensagem deliberadamente igual para código inexistente, expirado e já
    // usado: distingui-los diria a um atacante que ele acertou o código e
    // errou só o tempo.
    throw new ApiError(
      410,
      "Código inválido, expirado ou já utilizado. " +
        "Gere um novo no painel administrativo.",
    );
  }

  const syncToken = randomBytes(48).toString("base64url");
  const tokenHash = createHash("sha256").update(syncToken, "utf8").digest("hex");

  const stores = await sql<{ name: string; api_base_url: string | null }[]>`
    SELECT name, api_base_url FROM stores
     WHERE id = ${code.store_id} AND tenant_id = ${code.tenant_id}
  `;
  const store = stores[0];

  await withTenant(code.tenant_id, async (tx) => {
    await tx`
      INSERT INTO devices
        (id, tenant_id, store_id, label, token_hash, hostname, os, arch,
         activated_at, last_seen_at)
      VALUES (${code.device_id}, ${code.tenant_id}, ${code.store_id},
              ${code.label}, ${tokenHash}, ${body.fingerprint.hostname},
              ${body.fingerprint.os}, ${body.fingerprint.arch}, now(), now())
      ON CONFLICT (id) DO UPDATE
         SET token_hash   = EXCLUDED.token_hash,
             hostname     = EXCLUDED.hostname,
             os           = EXCLUDED.os,
             arch         = EXCLUDED.arch,
             activated_at = now(),
             revoked_at   = NULL
    `;

    if (body.device_secret_hex) {
      const secret = Buffer.from(body.device_secret_hex, "hex");
      // `DO NOTHING` e não `DO UPDATE`: reativar um terminal NÃO troca o
      // segredo do ledger. Trocá-lo invalidaria toda a cadeia já ancorada
      // daquele terminal de uma vez — e o sintoma seria o sistema acusando
      // fraude em histórico legítimo.
      await tx`
        INSERT INTO device_secrets (tenant_id, device_id, secret)
        VALUES (${code.tenant_id}, ${code.device_id}, ${secret})
        ON CONFLICT (tenant_id, device_id) DO NOTHING
      `;
    }
  });

  console.info("[ativacao] terminal ativado", {
    tenant: code.tenant_id,
    store: code.store_id,
    device: code.device_id,
    ip,
  });

  return json({
    tenant_id: code.tenant_id,
    store_id: code.store_id,
    device_id: code.device_id,
    sync_token: syncToken,
    store_name: store?.name ?? "",
    cloud_base_url: store?.api_base_url ?? "",
  });
});
