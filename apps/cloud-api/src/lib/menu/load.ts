/**
 * O cardápio público: do token impresso no QR até os pratos e as sugestões.
 *
 * É o único caminho da retaguarda que lê sem sessão nenhuma. A ordem é a
 * garantia:
 *
 * 1. **resolver o link** — `token` ativo, de tenant não suspenso. Sem isso,
 *    `null` (a página responde 404, igual para token inexistente e revogado:
 *    distinguir diria a quem tenta adivinhar quais tokens já existiram);
 * 2. **entrar no tenant daquela loja** (`withTenant`) e só então ler produto e
 *    venda. Nenhuma consulta de dado de negócio roda fora dele.
 *
 * O cliente vê nome, descrição, categoria e preço. Não vê quantidades nem
 * faturamento: a ordem de "mais pedidos" e das sugestões vem das vendas, mas
 * os números ficam aqui dentro.
 */

import { randomBytes } from "node:crypto";

import { sql, withTenant, type Tx } from "@/lib/db";

import { rankCompanions, type Companion, type PairCount } from "./upsell";

export interface MenuItem {
  id: string;
  name: string;
  description: string;
  category: string;
  priceCents: number;
  pricingMode: "unit" | "weight";
}

export interface MenuCategory {
  name: string;
  items: MenuItem[];
}

export interface MenuData {
  storeName: string;
  tableLabel: string | null;
  categories: MenuCategory[];
  /** Os mais pedidos dos últimos 30 dias, sem os números. */
  popular: string[];
  /** Para cada produto, o que costuma ir junto (ordem e confiança). */
  companions: Record<string, Companion[]>;
}

/** Nome da seção de quem não tem categoria. Vai por último. */
export const UNCATEGORIZED = "Outros";

/** Janelas das estatísticas. Curtas o bastante para acompanhar a estação. */
const PAIR_WINDOW_DAYS = 90;
const POPULAR_WINDOW_DAYS = 30;
const POPULAR_LIMIT = 6;

/**
 * Cache das estatísticas por loja. O cardápio é aberto por dezenas de mesas
 * na mesma hora; recalcular a coocorrência a cada abertura seria a consulta
 * mais cara do sistema rodando no horário de pico. Dez minutos é imperceptível
 * para uma sugestão e poupa o banco no rush.
 */
const STATS_TTL_MS = 10 * 60 * 1000;
const statsCache = new Map<string, { expires: number; stats: Stats }>();

interface Stats {
  orderCounts: Map<string, number>;
  popularCounts: Map<string, number>;
  pairs: PairCount[];
}

/** Um token novo: 144 bits aleatórios, 24 caracteres na URL. */
export function newMenuToken(): string {
  return randomBytes(18).toString("base64url");
}

/** A forma de um token válido — recusa lixo antes de consultar o banco. */
export function isPlausibleToken(token: string): boolean {
  return /^[A-Za-z0-9_-]{20,64}$/.test(token);
}

export async function loadMenu(token: string, now: Date = new Date()): Promise<MenuData | null> {
  if (!isPlausibleToken(token)) return null;

  // Fora de tenant, de propósito: é exatamente esta consulta que descobre qual
  // é o tenant. Ela lê só `menu_links`, `stores` e `tenants`.
  const [link] = await sql<{
    tenant_id: string;
    store_id: string;
    table_label: string | null;
    store_name: string;
  }[]>`
    SELECT l.tenant_id, l.store_id, l.table_label, s.name AS store_name
      FROM menu_links l
      JOIN stores s ON s.id = l.store_id AND s.tenant_id = l.tenant_id
      JOIN tenants t ON t.id = l.tenant_id
     WHERE l.token = ${token}
       AND l.revoked_at IS NULL
       AND t.suspended_at IS NULL
  `;
  if (!link) return null;

  return withTenant(link.tenant_id, async (tx) => {
    const products = await tx<{
      id: string;
      name: string;
      description: string | null;
      category: string | null;
      price_cents: string;
      pricing_mode: "unit" | "weight";
    }[]>`
      SELECT id::text, name, description, category, price_cents::text, pricing_mode
        FROM products
       WHERE tenant_id = ${link.tenant_id}
         AND is_active AND menu_visible
       ORDER BY name
    `;

    const items: MenuItem[] = products.map((p) => ({
      id: p.id,
      name: p.name,
      description: p.description ?? "",
      category: p.category?.trim() || UNCATEGORIZED,
      priceCents: Number(p.price_cents),
      pricingMode: p.pricing_mode,
    }));
    const eligible = new Set(items.map((item) => item.id));

    const stats = await storeStats(tx, link.tenant_id, link.store_id, now);
    const ranked = rankCompanions(stats.pairs, stats.orderCounts, eligible);

    const popular = [...stats.popularCounts.entries()]
      .filter(([id]) => eligible.has(id))
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, POPULAR_LIMIT)
      .map(([id]) => id);

    return {
      storeName: link.store_name,
      tableLabel: link.table_label,
      categories: groupByCategory(items),
      popular,
      companions: Object.fromEntries(ranked),
    };
  });
}

export function groupByCategory(items: readonly MenuItem[]): MenuCategory[] {
  const groups = new Map<string, MenuItem[]>();
  for (const item of items) {
    const list = groups.get(item.category) ?? [];
    list.push(item);
    groups.set(item.category, list);
  }
  return [...groups.entries()]
    .sort(([a], [b]) =>
      a === UNCATEGORIZED ? 1 : b === UNCATEGORIZED ? -1 : a.localeCompare(b, "pt-BR"),
    )
    .map(([name, list]) => ({ name, items: list }));
}

async function storeStats(tx: Tx, tenantId: string, storeId: string, now: Date): Promise<Stats> {
  const key = `${tenantId}:${storeId}`;
  const cached = statsCache.get(key);
  if (cached && cached.expires > now.getTime()) return cached.stats;

  const pairSince = new Date(now.getTime() - PAIR_WINDOW_DAYS * 86_400_000);
  const popularSince = new Date(now.getTime() - POPULAR_WINDOW_DAYS * 86_400_000);

  // Só venda PAGA e item NÃO cancelado: um combo que o cliente desistiu na
  // hora de pagar não é padrão de consumo, e cancelamento é justamente o que o
  // anti-furto marca como suspeito.
  const counts = await tx<{ product_id: string; orders: string; recent: string }[]>`
    SELECT oi.product_id,
           count(DISTINCT oi.order_id)::text AS orders,
           count(DISTINCT oi.order_id) FILTER (WHERE o.closed_at >= ${popularSince})::text AS recent
      FROM order_items oi
      JOIN orders o ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
     WHERE o.tenant_id = ${tenantId} AND o.store_id = ${storeId}
       AND o.status = 'paid' AND oi.canceled_at IS NULL
       AND oi.product_id IS NOT NULL
       AND o.closed_at >= ${pairSince}
     GROUP BY oi.product_id
  `;

  const pairs = await tx<{ source: string; target: string; together: string }[]>`
    SELECT a.product_id AS source, b.product_id AS target,
           count(DISTINCT a.order_id)::text AS together
      FROM order_items a
      JOIN order_items b
        ON b.tenant_id = a.tenant_id AND b.order_id = a.order_id
       AND b.product_id <> a.product_id AND b.canceled_at IS NULL
      JOIN orders o ON o.id = a.order_id AND o.tenant_id = a.tenant_id
     WHERE o.tenant_id = ${tenantId} AND o.store_id = ${storeId}
       AND o.status = 'paid' AND a.canceled_at IS NULL
       AND a.product_id IS NOT NULL AND b.product_id IS NOT NULL
       AND o.closed_at >= ${pairSince}
     GROUP BY a.product_id, b.product_id
  `;

  const stats: Stats = {
    orderCounts: new Map(counts.map((row) => [row.product_id, Number(row.orders)])),
    popularCounts: new Map(
      counts.filter((row) => Number(row.recent) > 0).map((row) => [row.product_id, Number(row.recent)]),
    ),
    pairs: pairs.map((row) => ({ from: row.source, to: row.target, together: Number(row.together) })),
  };
  statsCache.set(key, { expires: now.getTime() + STATS_TTL_MS, stats });
  return stats;
}

/** Para os testes, e para o painel logo depois de um ajuste. */
export function clearMenuCache(): void {
  statsCache.clear();
}
