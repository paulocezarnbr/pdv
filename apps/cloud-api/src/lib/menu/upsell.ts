/**
 * Upsell contextual do cardápio: "quem pediu isto também pediu aquilo".
 *
 * Sem LLM, de propósito. A sugestão sai das vendas da PRÓPRIA loja — o par
 * café + pão de queijo que acontece ali — e não de um modelo que conhece
 * restaurantes em geral. É barato, explicável ("porque 40% de quem pede X leva
 * Y") e não inventa um prato que a casa não vende.
 *
 * A medida é a confiança da regra X → Y: dos pedidos com X, quantos também
 * têm Y. Com dois freios:
 *
 * * **suporte mínimo** — dois pedidos juntos numa semana fraca não são
 *   padrão, são coincidência, e sugerir coincidência ensina o cliente a
 *   ignorar a sugestão;
 * * **o próprio item e o que já está na seleção nunca são sugeridos** —
 *   sugerir o que a pessoa já escolheu é o jeito mais rápido de parecer
 *   robô.
 */

export interface PairCount {
  /** Produto de origem. */
  from: string;
  /** Produto que apareceu no mesmo pedido. */
  to: string;
  /** Em quantos pedidos os dois apareceram juntos. */
  together: number;
}

export interface Companion {
  productId: string;
  /** Fração dos pedidos com a origem que também têm este produto. */
  confidence: number;
  together: number;
}

export interface RankOptions {
  /** Pedidos juntos, no mínimo, para o par contar. */
  minSupport?: number;
  /** Confiança mínima: abaixo disso é ruído mesmo com volume. */
  minConfidence?: number;
  /** Quantas sugestões por produto. */
  limit?: number;
}

export const DEFAULT_MIN_SUPPORT = 3;
export const DEFAULT_MIN_CONFIDENCE = 0.1;

/**
 * As melhores companhias de cada produto.
 *
 * @param orderCounts em quantos pedidos cada produto apareceu (o denominador).
 * @param eligible produtos que podem ser sugeridos (ativos e no cardápio).
 */
export function rankCompanions(
  pairs: readonly PairCount[],
  orderCounts: ReadonlyMap<string, number>,
  eligible: ReadonlySet<string>,
  options: RankOptions = {},
): Map<string, Companion[]> {
  const minSupport = options.minSupport ?? DEFAULT_MIN_SUPPORT;
  const minConfidence = options.minConfidence ?? DEFAULT_MIN_CONFIDENCE;
  const limit = options.limit ?? 3;

  const byOrigin = new Map<string, Companion[]>();
  for (const pair of pairs) {
    if (pair.from === pair.to) continue;
    if (!eligible.has(pair.from) || !eligible.has(pair.to)) continue;
    if (pair.together < minSupport) continue;
    const base = orderCounts.get(pair.from) ?? 0;
    if (base <= 0) continue;
    // Nunca acima de 1: contagens vindas de janelas diferentes não podem
    // produzir "120% de quem pede X".
    const confidence = Math.min(1, pair.together / base);
    if (confidence < minConfidence) continue;
    const list = byOrigin.get(pair.from) ?? [];
    list.push({ productId: pair.to, confidence, together: pair.together });
    byOrigin.set(pair.from, list);
  }

  for (const [origin, list] of byOrigin) {
    // Empate de confiança desempata por volume, e depois pelo id: a mesma
    // loja com os mesmos dados mostra sempre as mesmas sugestões.
    list.sort(
      (a, b) =>
        b.confidence - a.confidence ||
        b.together - a.together ||
        a.productId.localeCompare(b.productId),
    );
    byOrigin.set(origin, list.slice(0, limit));
  }
  return byOrigin;
}

/**
 * Sugestões para a seleção inteira: soma as confianças vindas de cada item.
 *
 * Quem escolheu café e bolo recebe primeiro o que combina com os DOIS, e não o
 * melhor par de um só deles. Roda no navegador, com o mapa que a página já
 * trouxe: montar a seleção não faz requisição nenhuma.
 */
export function suggestForSelection(
  companions: Readonly<Record<string, readonly Companion[]>>,
  selection: readonly string[],
  limit = 3,
): string[] {
  const chosen = new Set(selection);
  const scores = new Map<string, number>();
  for (const origin of chosen) {
    for (const companion of companions[origin] ?? []) {
      if (chosen.has(companion.productId)) continue;
      scores.set(
        companion.productId,
        (scores.get(companion.productId) ?? 0) + companion.confidence,
      );
    }
  }
  return [...scores.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit)
    .map(([productId]) => productId);
}
