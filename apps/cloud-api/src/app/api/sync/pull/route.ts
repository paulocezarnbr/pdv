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
import { PULLABLE, pullRows } from "@/lib/sync/pull";

export const dynamic = "force-dynamic";

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

  const rows = await pullRows(
    sql as never, { tenantId: device.tenantId, storeId: device.storeId }, table, since, limit,
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
