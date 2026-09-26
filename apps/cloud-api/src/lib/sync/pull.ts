/**
 * O que o terminal pode baixar, e de quem.
 *
 * Separado da rota para ser testado contra o Postgres, com o RLS ligado: a
 * rota é só autenticação e parâmetros.
 */

import type { Tx } from "@/lib/db";

/**
 * Lista fechada, e só cadastro.
 *
 * `users` está aqui porque é o que permite validar o PIN offline: sem a
 * réplica local, uma queda de internet impediria de abrir o caixa.
 *
 * `customers` é do ESTABELECIMENTO: desce para os caixas da loja em que foi
 * cadastrado. O cadastro antigo, sem loja (antes da migração 021), desce para
 * todos os caixas da rede, como era visto antes.
 */
export const PULLABLE = new Set([
  "products",
  "recipes",
  "recipe_lines",
  "inventory_items",
  "users",
  "customers",
]);

/** Tabelas que pertencem a uma loja: o caixa só vê as linhas da dele. */
const PER_STORE = new Set(["customers"]);

export interface PullScope {
  tenantId: string;
  storeId: string;
}

export async function pullRows(
  db: Tx,
  scope: PullScope,
  table: string,
  since: number,
  limit: number,
): Promise<Record<string, unknown>[]> {
  if (!PULLABLE.has(table)) throw new Error(`tabela fora da lista: ${table}`);
  // O nome da tabela passou pela lista branca acima; nada mais é interpolado.
  if (PER_STORE.has(table)) {
    return db.unsafe<Record<string, unknown>[]>(
      `SELECT * FROM ${table}
        WHERE tenant_id = $1 AND (store_id = $2 OR store_id IS NULL) AND server_seq > $3
        ORDER BY server_seq
        LIMIT $4`,
      [scope.tenantId, scope.storeId, since, limit] as never[],
    );
  }
  return db.unsafe<Record<string, unknown>[]>(
    `SELECT * FROM ${table}
      WHERE tenant_id = $1 AND server_seq > $2
      ORDER BY server_seq
      LIMIT $3`,
    [scope.tenantId, since, limit] as never[],
  );
}
