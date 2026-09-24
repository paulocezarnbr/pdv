/**
 * A previsão da semana de uma loja: do histórico de vendas às sugestões.
 *
 * Roda dentro do tenant (`withTenant`), como toda leitura de negócio. As séries
 * são por DIA LOCAL da loja: a janta que fecha às 22h de sábado é venda de
 * sábado, e em UTC ela cairia no domingo — o sazonal aprenderia que domingo
 * vende o jantar de sábado.
 *
 * Duas séries por loja:
 *
 * * **produto** — quantas unidades (ou kg, se vendido por peso) saíram por dia,
 *   de pedidos PAGOS e itens NÃO cancelados;
 * * **insumo** — quanto foi consumido por dia, pela baixa que o caixa grava em
 *   cada item vendido (`order_item_ingredients`). É o consumo real, não a
 *   receita de hoje aplicada ao passado: receita muda, e o que saiu da
 *   prateleira em agosto foi o que a receita de agosto mandou.
 */

import type { Tx } from "@/lib/db";

import {
  HORIZON,
  calendarFrom,
  forecast,
  purchaseSuggestion,
  type Method,
  type Series,
} from "./model";

/** O produto é 100% Brasil; `stores` não tem fuso próprio. */
export const STORE_TIME_ZONE = "America/Sao_Paulo";
/** 26 semanas: pega a estação sem arrastar o cardápio do ano passado. */
const HISTORY_DAYS = 182;
/** Só entra na previsão quem vendeu nas últimas 4 semanas. */
const ACTIVE_DAYS = 28;
const MAX_PRODUCTS = 40;
const MAX_INGREDIENTS = 40;
const CACHE_TTL_MS = 30 * 60 * 1000;

export interface DayValue {
  day: string;
  value: number;
}

export interface ProductForecast {
  key: string;
  name: string;
  unit: "un" | "kg";
  method: Method;
  days: DayValue[];
  total: number;
  /** Erro médio do método escolhido nas duas últimas semanas (fração). */
  error: number | null;
}

export interface IngredientForecast {
  id: string;
  name: string;
  method: Method;
  needKg: number;
  safetyKg: number;
  /** `null`: nenhuma contagem lançada — o saldo é desconhecido. */
  balanceKg: number | null;
  countedAt: string | null;
  buyKg: number | null;
  coverageDays: number | null;
  error: number | null;
}

export interface StoreForecast {
  storeId: string;
  generatedAt: string;
  /** Dias em que a loja abriu dentro da janela de histórico. */
  openDays: number;
  days: string[];
  products: ProductForecast[];
  ingredients: IngredientForecast[];
}

const cache = new Map<string, { expires: number; value: StoreForecast }>();

export function clearForecastCache(tenantId?: string, storeId?: string): void {
  if (!tenantId) return cache.clear();
  cache.delete(`${tenantId}:${storeId}`);
}

function addDays(day: string, delta: number): string {
  const date = new Date(Date.parse(`${day}T12:00:00Z`) + delta * 86_400_000);
  return date.toISOString().slice(0, 10);
}

/** A data de hoje no fuso da loja. */
export function localToday(now: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: STORE_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
}

export async function loadForecast(
  tx: Tx,
  tenantId: string,
  storeId: string,
  now: Date = new Date(),
): Promise<StoreForecast> {
  const key = `${tenantId}:${storeId}`;
  const cached = cache.get(key);
  if (cached && cached.expires > now.getTime()) return cached.value;

  // Hoje fica FORA do histórico: o dia pela metade pareceria um dia fraco, e o
  // sazonal ensinaria a loja a esperar metade do movimento.
  const today = localToday(now);
  const first = addDays(today, -HISTORY_DAYS);
  const length = HISTORY_DAYS;
  const indexOf = new Map(Array.from({ length }, (_, i) => [addDays(first, i), i]));
  const tz = STORE_TIME_ZONE;

  const open = await tx<{ day: string }[]>`
    SELECT DISTINCT to_char((o.closed_at AT TIME ZONE ${tz})::date, 'YYYY-MM-DD') AS day
      FROM orders o
     WHERE o.tenant_id = ${tenantId} AND o.store_id = ${storeId} AND o.status = 'paid'
       AND o.closed_at >= (${first}::date)::timestamp AT TIME ZONE ${tz}
       AND o.closed_at <  (${today}::date)::timestamp AT TIME ZONE ${tz}
  `;
  const openDays = new Set(open.map((row) => row.day));

  const sold = await tx<{ key: string; name: string; weighed: boolean; day: string; amount: string }[]>`
    SELECT coalesce(oi.product_id, 'nome:' || oi.product_name) AS key,
           coalesce(max(p.name), max(oi.product_name)) AS name,
           bool_or(oi.pricing_mode = 'weight') AS weighed,
           to_char((o.closed_at AT TIME ZONE ${tz})::date, 'YYYY-MM-DD') AS day,
           sum(CASE WHEN oi.pricing_mode = 'weight' THEN oi.net_weight_grams / 1000.0
                    WHEN oi.quantity ~ '^[0-9]+(\\.[0-9]+)?$' THEN oi.quantity::numeric
                    ELSE 1 END)::text AS amount
      FROM order_items oi
      JOIN orders o ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
      LEFT JOIN products p ON p.tenant_id = oi.tenant_id AND p.id::text = oi.product_id
     WHERE o.tenant_id = ${tenantId} AND o.store_id = ${storeId} AND o.status = 'paid'
       AND oi.canceled_at IS NULL
       AND o.closed_at >= (${first}::date)::timestamp AT TIME ZONE ${tz}
       AND o.closed_at <  (${today}::date)::timestamp AT TIME ZONE ${tz}
     GROUP BY 1, 4
  `;

  const consumed = await tx<{ id: string; name: string; day: string; mg: string }[]>`
    SELECT i.inventory_item_id AS id,
           max(nullif(i.inventory_item_name, '')) AS name,
           to_char((o.closed_at AT TIME ZONE ${tz})::date, 'YYYY-MM-DD') AS day,
           sum(i.consumed_mg)::text AS mg
      FROM order_item_ingredients i
      JOIN order_items oi ON oi.id = i.order_item_id AND oi.tenant_id = i.tenant_id
      JOIN orders o ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
     WHERE o.tenant_id = ${tenantId} AND o.store_id = ${storeId} AND o.status = 'paid'
       AND oi.canceled_at IS NULL
       AND o.closed_at >= (${first}::date)::timestamp AT TIME ZONE ${tz}
       AND o.closed_at <  (${today}::date)::timestamp AT TIME ZONE ${tz}
     GROUP BY 1, 3
  `;

  // Saldo = última contagem + o que se moveu depois dela. Sem contagem, nulo.
  const balances = await tx<{ id: string; name: string; counted_at: Date; balance_mg: string }[]>`
    SELECT c.inventory_item_id AS id, c.inventory_item_name AS name,
           c.counted_at,
           (c.counted_mg + coalesce((
              SELECT sum(m.quantity_mg) FROM stock_movements m
               WHERE m.tenant_id = c.tenant_id AND m.store_id = c.store_id
                 AND m.inventory_item_id = c.inventory_item_id
                 AND m.created_at > c.counted_at
           ), 0))::text AS balance_mg
      FROM (
        SELECT DISTINCT ON (inventory_item_id) *
          FROM inventory_counts
         WHERE tenant_id = ${tenantId} AND store_id = ${storeId}
         ORDER BY inventory_item_id, counted_at DESC
      ) c
  `;

  const calendar = calendarFrom(first);
  const days = Array.from({ length: HORIZON }, (_, i) => addDays(today, i));
  const activeFrom = length - ACTIVE_DAYS;

  function build<T extends { day: string }>(rows: T[], key: (row: T) => string, value: (row: T) => number) {
    const series = new Map<string, (number | null)[]>();
    for (const row of rows) {
      const index = indexOf.get(row.day);
      if (index === undefined) continue;
      const id = key(row);
      let list = series.get(id);
      if (!list) {
        // Dia aberto sem venda do produto é zero; dia fechado é ausência.
        list = Array.from({ length }, (_, i) => (openDays.has(addDays(first, i)) ? 0 : null));
        series.set(id, list);
      }
      list[index] = (list[index] ?? 0) + value(row);
    }
    return series;
  }

  const recentVolume = (list: Series) =>
    list.slice(activeFrom).reduce<number>((sum, v) => sum + (v ?? 0), 0);

  const productMeta = new Map<string, { name: string; weighed: boolean }>();
  for (const row of sold) {
    const meta = productMeta.get(row.key) ?? { name: row.name, weighed: false };
    meta.weighed ||= row.weighed;
    productMeta.set(row.key, meta);
  }
  const productSeries = [...build(sold, (r) => r.key, (r) => Number(r.amount)).entries()]
    .filter(([, list]) => recentVolume(list) > 0)
    .sort((a, b) => recentVolume(b[1]) - recentVolume(a[1]))
    .slice(0, MAX_PRODUCTS);

  const products: ProductForecast[] = productSeries.map(([id, list]) => {
    const result = forecast(list, calendar);
    const meta = productMeta.get(id)!;
    return {
      key: id,
      name: meta.name,
      unit: meta.weighed ? "kg" : "un",
      method: result.method,
      days: days.map((day, i) => ({ day, value: round(result.values[i]!, meta.weighed ? 2 : 1) })),
      total: round(result.values.reduce((a, b) => a + b, 0), meta.weighed ? 2 : 0),
      error: chosenError(result),
    };
  });

  const ingredientNames = new Map<string, string>();
  for (const row of consumed) if (row.name) ingredientNames.set(row.id, row.name);
  const balanceOf = new Map(balances.map((row) => [row.id, row]));
  for (const row of balances) if (!ingredientNames.has(row.id)) ingredientNames.set(row.id, row.name);

  const ingredientSeries = [...build(consumed, (r) => r.id, (r) => Number(r.mg) / 1_000_000).entries()]
    .filter(([, list]) => recentVolume(list) > 0)
    .sort((a, b) => recentVolume(b[1]) - recentVolume(a[1]))
    .slice(0, MAX_INGREDIENTS);

  const ingredients: IngredientForecast[] = ingredientSeries.map(([id, list]) => {
    const result = forecast(list, calendar);
    const counted = balanceOf.get(id);
    const balanceKg = counted ? Number(counted.balance_mg) / 1_000_000 : null;
    const plan = purchaseSuggestion({ forecast: result.values, errorSd: result.errorSd, balance: balanceKg });
    return {
      id,
      name: ingredientNames.get(id) ?? "Insumo sem nome",
      method: result.method,
      needKg: round(plan.need, 3),
      safetyKg: round(plan.safety, 3),
      balanceKg: balanceKg === null ? null : round(balanceKg, 3),
      countedAt: counted ? new Date(counted.counted_at).toISOString() : null,
      buyKg: plan.buy === null ? null : round(plan.buy, 3),
      coverageDays: plan.coverageDays === null ? null : round(plan.coverageDays, 1),
      error: chosenError(result),
    };
  });
  // Quem precisa de compra primeiro; sem contagem, depois; o resto por último.
  ingredients.sort((a, b) => rank(a) - rank(b) || (b.buyKg ?? 0) - (a.buyKg ?? 0) || b.needKg - a.needKg);

  const value: StoreForecast = {
    storeId,
    generatedAt: now.toISOString(),
    openDays: openDays.size,
    days,
    products,
    ingredients,
  };
  cache.set(key, { expires: now.getTime() + CACHE_TTL_MS, value });
  return value;
}

function rank(item: IngredientForecast): number {
  if (item.buyKg !== null && item.buyKg > 0) return 0;
  if (item.buyKg === null) return 1;
  return 2;
}

function chosenError(result: ReturnType<typeof forecast>): number | null {
  const error = result.method === "boosting" ? result.backtest.boosting : result.backtest.sazonal;
  return error === null ? null : round(error, 3);
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}
