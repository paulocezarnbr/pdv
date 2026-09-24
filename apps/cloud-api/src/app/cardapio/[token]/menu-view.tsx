"use client";

/**
 * O cardápio no celular do cliente.
 *
 * "Minha seleção" não é pedido: é uma lista para mostrar ao garçom. O pedido
 * continua sendo lançado por ele, no app do salão — um segundo caminho de
 * pedido, sem ninguém da casa conferindo, abriria a mesma porta que o PDV
 * fecha no balcão (comanda que ninguém viu). Pedido direto pela mesa exige a
 * integração com o caixa e fica para uma etapa própria.
 *
 * As sugestões da seleção são calculadas aqui, com o mapa que a página já
 * trouxe: montar a lista não faz requisição nenhuma.
 */

import { useMemo, useState } from "react";

import type { MenuData, MenuItem } from "@/lib/menu/load";
import { suggestForSelection } from "@/lib/menu/upsell";

const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

function price(item: MenuItem): string {
  const value = money.format(item.priceCents / 100);
  return item.pricingMode === "weight" ? `${value} / kg` : value;
}

export function MenuView({ menu }: { menu: MenuData }) {
  const byId = useMemo(() => {
    const map = new Map<string, MenuItem>();
    for (const category of menu.categories) for (const item of category.items) map.set(item.id, item);
    return map;
  }, [menu]);

  const [open, setOpen] = useState<string | null>(null);
  const [selection, setSelection] = useState<Record<string, number>>({});
  const [sheet, setSheet] = useState(false);

  const chosen = Object.keys(selection).filter((id) => (selection[id] ?? 0) > 0);
  const count = chosen.reduce((sum, id) => sum + (selection[id] ?? 0), 0);
  // Item por peso não entra no total: quem pesa é a balança do balcão.
  const total = chosen.reduce((sum, id) => {
    const item = byId.get(id);
    return item && item.pricingMode === "unit" ? sum + item.priceCents * (selection[id] ?? 0) : sum;
  }, 0);
  const hasWeighed = chosen.some((id) => byId.get(id)?.pricingMode === "weight");
  const suggestions = suggestForSelection(menu.companions, chosen)
    .map((id) => byId.get(id))
    .filter((item): item is MenuItem => Boolean(item));

  const add = (id: string) => setSelection((current) => ({ ...current, [id]: (current[id] ?? 0) + 1 }));
  const remove = (id: string) =>
    setSelection((current) => ({ ...current, [id]: Math.max(0, (current[id] ?? 0) - 1) }));

  const popular = menu.popular.map((id) => byId.get(id)).filter((item): item is MenuItem => Boolean(item));
  const empty = menu.categories.length === 0;

  return (
    <div className="menu-page">
      <header className="menu-header">
        <p className="menu-kicker">Cardápio{menu.tableLabel ? ` · ${menu.tableLabel}` : ""}</p>
        <h1>{menu.storeName}</h1>
      </header>

      {!empty && (
        <nav className="menu-tabs" aria-label="Categorias">
          {menu.categories.map((category) => (
            <a key={category.name} href={`#${anchor(category.name)}`}>{category.name}</a>
          ))}
        </nav>
      )}

      <main className="menu-main">
        {empty && <p className="menu-empty">O cardápio desta casa ainda está sendo preparado.</p>}

        {popular.length > 0 && (
          <section className="menu-section" aria-labelledby="mais-pedidos">
            <h2 id="mais-pedidos">Mais pedidos</h2>
            <div className="menu-strip">
              {popular.map((item) => (
                <button key={item.id} type="button" className="menu-chip" onClick={() => add(item.id)}>
                  <span>{item.name}</span>
                  <small>{price(item)}</small>
                </button>
              ))}
            </div>
          </section>
        )}

        {menu.categories.map((category) => (
          <section key={category.name} id={anchor(category.name)} className="menu-section">
            <h2>{category.name}</h2>
            <ul className="menu-list">
              {category.items.map((item) => {
                const companions = (menu.companions[item.id] ?? [])
                  .map((companion) => byId.get(companion.productId))
                  .filter((other): other is MenuItem => Boolean(other));
                const expanded = open === item.id;
                return (
                  <li key={item.id} className="menu-item">
                    <button
                      type="button"
                      className="menu-item-main"
                      aria-expanded={expanded}
                      onClick={() => setOpen(expanded ? null : item.id)}
                    >
                      <span className="menu-item-text">
                        <strong>{item.name}</strong>
                        {item.description && <span>{item.description}</span>}
                      </span>
                      <span className="menu-price">{price(item)}</span>
                    </button>
                    <button
                      type="button"
                      className="menu-add"
                      aria-label={`Adicionar ${item.name} à seleção`}
                      onClick={() => add(item.id)}
                    >
                      {selection[item.id] ? selection[item.id] : "+"}
                    </button>
                    {expanded && companions.length > 0 && (
                      <div className="menu-pairs">
                        <small>Quem pede este também pede</small>
                        {companions.map((other) => (
                          <button key={other.id} type="button" onClick={() => add(other.id)}>
                            + {other.name} <em>{price(other)}</em>
                          </button>
                        ))}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
        <p className="menu-footnote">Preços em reais. Itens por peso são pesados no balcão.</p>
      </main>

      {count > 0 && (
        <button type="button" className="menu-bar" onClick={() => setSheet(true)}>
          <span>Minha seleção · {count} {count === 1 ? "item" : "itens"}</span>
          <strong>{money.format(total / 100)}{hasWeighed ? " + peso" : ""}</strong>
        </button>
      )}

      {sheet && (
        <div className="menu-sheet" role="dialog" aria-modal="true" aria-label="Minha seleção">
          <div className="menu-sheet-body">
            <div className="menu-sheet-head">
              <h2>Minha seleção</h2>
              <button type="button" onClick={() => setSheet(false)}>Fechar</button>
            </div>
            <ul className="menu-list">
              {chosen.map((id) => {
                const item = byId.get(id);
                if (!item) return null;
                return (
                  <li key={id} className="menu-line">
                    <span>{item.name}</span>
                    <span className="menu-qty">
                      <button type="button" aria-label="Menos" onClick={() => remove(id)}>−</button>
                      <b>{selection[id]}</b>
                      <button type="button" aria-label="Mais" onClick={() => add(id)}>+</button>
                    </span>
                  </li>
                );
              })}
            </ul>
            {suggestions.length > 0 && (
              <div className="menu-pairs">
                <small>Combina com a sua seleção</small>
                {suggestions.map((item) => (
                  <button key={item.id} type="button" onClick={() => add(item.id)}>
                    + {item.name} <em>{price(item)}</em>
                  </button>
                ))}
              </div>
            )}
            <p className="menu-total">
              Total estimado <strong>{money.format(total / 100)}</strong>
              {hasWeighed && <small> + itens por peso</small>}
            </p>
            <p className="menu-hint">Mostre esta lista ao garçom — o pedido é feito com ele.</p>
          </div>
        </div>
      )}
    </div>
  );
}

function anchor(name: string): string {
  return `c-${name.normalize("NFD").replace(/[^\w]+/g, "-").toLowerCase()}`;
}
