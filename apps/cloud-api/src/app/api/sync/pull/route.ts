/**
 * `GET /api/sync/pull` — o terminal baixa os cadastros da retaguarda.
 *
 * Direção oposta ao `push`, e por isso com riscos opostos: aqui o perigo não é
 * um terminal escrever onde não deve, é um terminal **ler** o que não é dele.
 * Duas travas:
 *
 * * O `tenant_id` sai do token, e a consulta filtra por ele. Sempre.
 * * A tabela vem por nome, do cliente, então passa por lista branca — e a
 *   lista só tem cadastro. `orders`, `payments` e `audit_ledger` ficam de
 *   fora: o terminal já tem os dele, e os dos outros terminais da rede não são
 *   assunto dele.
 *
 * Por que paginar por `server_seq` e não por `updated_at`
 * -------------------------------------------------------
 *
 * Dois registros podem compartilhar o mesmo timestamp. Com `updated_at` como
 * cursor, um deles cai exatamente na virada de página e é pulado — para sempre,
 * porque o cursor já passou. `server_seq` vem de uma sequência do Postgres: é
 * estritamente crescente e não empata.
 */

import { requireDevice } from "@/lib/auth/device";
import { sql } from "@/lib/db";
import { ApiError, handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

/**
 * O que o terminal pode baixar. Lista fechada, e só cadastro.
 *
 * `users` está aqui porque é o que permite validar o PIN offline: sem a
 * réplica local, uma queda de internet impediria de abrir o caixa — que é o
 * oposto do que o sistema inteiro existe para garantir.
 */
const PULLABLE = new Set([
  "products",
  "recipes",
  "recipe_lines",
  "inventory_items",
  "users",
]);

export const GET = handler(async (request) => {
  const device = await requireDevice(request);
  const params = new URL(request.url).searchParams;

  const table = params.get("entity_table") ?? "";
  if (!PULLABLE.has(table)) {
    throw new ApiError(400, `Tabela não disponível: ${table}`);
  }

  const since = Number.parseInt(params.get("since") ?? "0", 10);
  const limit = Number.parseInt(params.get("limit") ?? "500", 10);
  if (!Number.isInteger(since) || since < 0) {
    throw new ApiError(400, "Cursor inválido.");
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    throw new ApiError(400, "limit precisa estar entre 1 e 1000.");
  }

  // O nome da tabela passou pela lista branca acima; nada mais é interpolado.
  const rows = await sql.unsafe<Record<string, unknown>[]>(
    `SELECT * FROM ${table}
      WHERE tenant_id = $1 AND server_seq > $2
      ORDER BY server_seq
      LIMIT $3`,
    [device.tenantId, since, limit] as never[],
  );

  const lastSeq = rows.reduce(
    (max, row) => Math.max(max, Number(row["server_seq"] ?? 0)),
    since,
  );

  return json({
    rows,
    last_server_seq: lastSeq,
    // `has_more` é derivado do tamanho da página, e não de um `COUNT(*)`:
    // contar a tabela inteira a cada página faria o custo crescer com o
    // catálogo do cliente, na rota que roda a cada ciclo de sync.
    has_more: rows.length === limit,
  });
});
