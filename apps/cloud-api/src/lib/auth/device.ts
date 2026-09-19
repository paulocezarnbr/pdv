/**
 * Quem é o terminal que está falando.
 *
 * A regra que sustenta a multi-tenancy
 * ------------------------------------
 *
 * **O `tenant_id` vem do token, nunca do corpo.** É a linha mais importante
 * deste arquivo. Se o `tenant_id` do payload fosse aceito, um terminal
 * comprometido gravaria vendas no tenant de outro restaurante — ou leria as
 * dele. Todo handler que escreve recebe o tenant daqui e ignora o do corpo;
 * quando o corpo traz um divergente, isso é sinal de terminal clonado e vira
 * 403, não correção silenciosa.
 *
 * O token é guardado por hash
 * ---------------------------
 *
 * O banco tem `token_hash`, nunca o token. Vazou o dump, os terminais
 * continuam precisando do token cru para falar — e o vazamento não dá a
 * ninguém a capacidade de sincronizar em nome de uma loja.
 */

import { createHash, timingSafeEqual } from "node:crypto";

import { sql } from "@/lib/db";
import { ApiError } from "@/lib/http";

export interface DeviceContext {
  tenantId: string;
  storeId: string;
  deviceId: string;
  storeName: string;
}

export function hashToken(token: string): string {
  return createHash("sha256").update(token.trim(), "utf8").digest("hex");
}

function extractBearer(request: Request): string | null {
  const header = request.headers.get("authorization");
  if (!header) return null;
  const [scheme, value] = header.split(" ");
  if (!scheme || scheme.toLowerCase() !== "bearer" || !value) return null;
  return value.trim() || null;
}

/**
 * Resolve o token num terminal ativo.
 *
 * @throws ApiError 401 — ausente, desconhecido ou revogado. Uma mensagem só
 *   para os três casos: distinguir "não existe" de "foi revogado" diria a quem
 *   está sondando que aquele token já foi válido em algum momento.
 */
export async function requireDevice(request: Request): Promise<DeviceContext> {
  const token = extractBearer(request);
  if (!token) {
    throw new ApiError(401, "Terminal não autenticado.", {
      "WWW-Authenticate": "Bearer",
    });
  }

  const digest = hashToken(token);
  const rows = await sql<
    {
      id: string;
      tenant_id: string;
      store_id: string;
      token_hash: string;
      revoked_at: Date | null;
      store_name: string | null;
    }[]
  >`
    SELECT d.id, d.tenant_id, d.store_id, d.token_hash, d.revoked_at,
           s.name AS store_name
      FROM devices d
      LEFT JOIN stores s ON s.id = d.store_id AND s.tenant_id = d.tenant_id
     WHERE d.token_hash = ${digest}
     LIMIT 1
  `;

  const row = rows[0];
  if (!row || row.revoked_at !== null) {
    throw new ApiError(401, "Terminal não reconhecido. Ative-o novamente.");
  }

  // Comparação em tempo constante mesmo já tendo casado no WHERE: a busca é
  // por índice e o retorno precisa passar por aqui para não transformar o
  // banco num oráculo de timing.
  const stored = Buffer.from(row.token_hash, "utf8");
  const offered = Buffer.from(digest, "utf8");
  if (stored.length !== offered.length || !timingSafeEqual(stored, offered)) {
    throw new ApiError(401, "Terminal não reconhecido.");
  }

  // `last_seen_at` sem esperar: é telemetria, e segurar a resposta do
  // `/sync/push` por um UPDATE de diagnóstico seria pagar latência na rota
  // mais quente do sistema para melhorar uma tela que ninguém olha em tempo
  // real.
  void sql`UPDATE devices SET last_seen_at = now() WHERE id = ${row.id}`.catch(
    (error: unknown) => console.warn("[device] last_seen_at falhou", error),
  );

  return {
    tenantId: row.tenant_id,
    storeId: row.store_id,
    deviceId: row.id,
    storeName: row.store_name ?? "",
  };
}

/**
 * O segredo HMAC do ledger daquele terminal.
 *
 * Fica numa tabela à parte (`device_secrets`) e não em `devices`, para que a
 * consulta de autenticação — que roda em **toda** requisição — nunca traga o
 * segredo junto. Uma coluna a mais no SELECT quente é uma cópia a mais do
 * segredo circulando pela memória do processo a cada chamada.
 */
export async function deviceSecret(
  tenantId: string,
  deviceId: string,
): Promise<Buffer> {
  const rows = await sql<{ secret: Buffer }[]>`
    SELECT secret FROM device_secrets
     WHERE tenant_id = ${tenantId} AND device_id = ${deviceId}
     LIMIT 1
  `;
  const row = rows[0];
  if (!row) {
    throw new ApiError(
      409,
      "Terminal sem segredo de auditoria provisionado. " +
        "Reative o terminal no painel.",
    );
  }
  return Buffer.isBuffer(row.secret) ? row.secret : Buffer.from(row.secret);
}
