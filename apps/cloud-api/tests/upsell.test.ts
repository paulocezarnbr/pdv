/**
 * As regras do upsell, sem banco. O que elas guardam é a sugestão não virar
 * ruído: coincidência não é padrão, e sugerir o que a pessoa já escolheu é o
 * jeito mais rápido de o cliente parar de ler as sugestões.
 */

import { describe, expect, it } from "vitest";

import { groupByCategory, isPlausibleToken, newMenuToken, UNCATEGORIZED } from "../src/lib/menu/load.ts";
import { rankCompanions, suggestForSelection } from "../src/lib/menu/upsell.ts";

const all = new Set(["cafe", "pao", "bolo", "suco", "oculto"]);

describe("companhias de cada produto", () => {
  const counts = new Map([["cafe", 100], ["pao", 60], ["bolo", 20]]);

  it("ordena pela fração de quem pediu a origem", () => {
    const ranked = rankCompanions(
      [
        { from: "cafe", to: "pao", together: 40 },
        { from: "cafe", to: "bolo", together: 15 },
      ],
      counts,
      all,
    );
    expect(ranked.get("cafe")).toEqual([
      { productId: "pao", confidence: 0.4, together: 40 },
      { productId: "bolo", confidence: 0.15, together: 15 },
    ]);
  });

  it("coincidência abaixo do suporte mínimo não vira sugestão", () => {
    const ranked = rankCompanions([{ from: "bolo", to: "suco", together: 2 }], counts, all);
    expect(ranked.get("bolo")).toBeUndefined();
  });

  it("confiança baixa não vira sugestão, mesmo com volume", () => {
    const ranked = rankCompanions([{ from: "cafe", to: "suco", together: 5 }], counts, all);
    expect(ranked.get("cafe")).toBeUndefined();
  });

  it("produto fora do cardápio nunca é sugerido nem sugere", () => {
    const eligible = new Set(["cafe", "pao"]);
    const ranked = rankCompanions(
      [
        { from: "cafe", to: "oculto", together: 50 },
        { from: "oculto", to: "cafe", together: 50 },
      ],
      new Map([["cafe", 100], ["oculto", 60]]),
      eligible,
    );
    expect(ranked.size).toBe(0);
  });

  it("nunca passa de 100%, mesmo com contagens de janelas diferentes", () => {
    const ranked = rankCompanions([{ from: "bolo", to: "cafe", together: 30 }], counts, all);
    expect(ranked.get("bolo")?.[0]?.confidence).toBe(1);
  });

  it("empate desempata sempre igual: mesma loja, mesmas sugestões", () => {
    const pairs = [
      { from: "cafe", to: "suco", together: 20 },
      { from: "cafe", to: "bolo", together: 20 },
    ];
    const first = rankCompanions(pairs, counts, all).get("cafe");
    const second = rankCompanions([...pairs].reverse(), counts, all).get("cafe");
    expect(first).toEqual(second);
    expect(first?.map((c) => c.productId)).toEqual(["bolo", "suco"]);
  });

  it("o próprio produto não é companhia dele mesmo", () => {
    const ranked = rankCompanions([{ from: "cafe", to: "cafe", together: 90 }], counts, all);
    expect(ranked.size).toBe(0);
  });
});

describe("sugestão para a seleção", () => {
  const companions = {
    cafe: [{ productId: "pao", confidence: 0.4, together: 40 }, { productId: "bolo", confidence: 0.3, together: 30 }],
    suco: [{ productId: "bolo", confidence: 0.5, together: 25 }, { productId: "cafe", confidence: 0.2, together: 10 }],
  };

  it("o que combina com os DOIS itens vem primeiro", () => {
    expect(suggestForSelection(companions, ["cafe", "suco"])).toEqual(["bolo", "pao"]);
  });

  it("nunca sugere o que já está na seleção", () => {
    expect(suggestForSelection(companions, ["suco", "bolo"])).toEqual(["cafe"]);
  });

  it("seleção vazia não sugere nada", () => {
    expect(suggestForSelection(companions, [])).toEqual([]);
  });
});

describe("cardápio", () => {
  it("categorias em ordem alfabética, com os sem categoria por último", () => {
    const item = (id: string, category: string) => ({
      id, name: id, description: "", category, priceCents: 100, pricingMode: "unit" as const,
    });
    const groups = groupByCategory([item("a", UNCATEGORIZED), item("b", "Salgados"), item("c", "Bebidas")]);
    expect(groups.map((g) => g.name)).toEqual(["Bebidas", "Salgados", UNCATEGORIZED]);
  });

  it("o token é aleatório, longo e seguro para URL", () => {
    const tokens = new Set(Array.from({ length: 200 }, () => newMenuToken()));
    expect(tokens.size).toBe(200);
    for (const token of tokens) expect(isPlausibleToken(token)).toBe(true);
  });

  it("recusa token com cara de ataque antes de consultar o banco", () => {
    expect(isPlausibleToken("../../etc/passwd")).toBe(false);
    expect(isPlausibleToken("abc")).toBe(false);
    expect(isPlausibleToken("a".repeat(65))).toBe(false);
    expect(isPlausibleToken("'; DROP TABLE menu_links; --xxxx")).toBe(false);
  });
});
