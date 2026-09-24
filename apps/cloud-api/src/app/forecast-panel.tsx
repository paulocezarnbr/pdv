"use client";

/**
 * Previsão da semana no painel — o que vai sair e o que comprar.
 *
 * A tela diz sempre DE ONDE veio cada número: o método (sazonal ou boosting) e
 * o erro que ele teve nas duas últimas semanas desta loja. Uma previsão sem o
 * tamanho do erro é lida como certeza, e aí a primeira semana que errar faz o
 * dono parar de olhar.
 *
 * A compra só é sugerida com saldo conhecido. Sem contagem lançada, a linha
 * pede a contagem em vez de inventar — sugerir compra contra um saldo que
 * ninguém mediu faria comprar o que já está na prateleira.
 */

import { Button, InlineLoading, Select, SelectItem, TextInput } from "@carbon/react";
import { Box, ChartLineData, Checkmark } from "@carbon/icons-react";
import { FormEvent, useCallback, useEffect, useState, type ReactNode } from "react";

import { parseKg } from "@/lib/forecast/input";

import { fail, toast } from "./alerts";

type DayValue = { day: string; value: number };
type Product = {
  key: string; name: string; unit: "un" | "kg"; method: string;
  days: DayValue[]; total: number; error: number | null;
};
type Ingredient = {
  id: string; name: string; method: string; needKg: number; safetyKg: number;
  balanceKg: number | null; countedAt: string | null; buyKg: number | null;
  coverageDays: number | null; error: number | null;
};
type Forecast = { openDays: number; days: string[]; products: Product[]; ingredients: Ingredient[]; generatedAt: string };
type State = { store: { id: string; name: string } | null; forecast: Forecast | null; can_count: boolean };

const WEEKDAY = ["D", "S", "T", "Q", "Q", "S", "S"];
const WEEKDAY_NAME = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"];
const MIN_OPEN_DAYS = 14;

const kg = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2, minimumFractionDigits: 0 });
const units = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });

function weekday(day: string): number {
  return new Date(`${day}T12:00:00Z`).getUTCDay();
}

function amount(value: number, unit: "un" | "kg"): string {
  return unit === "kg" ? `${kg.format(value)} kg` : `${units.format(value)} un`;
}

function methodNote(method: string, error: number | null): string {
  if (method === "sem-historico") return "Sem histórico";
  const name = method === "boosting" ? "Boosting" : "Sazonal";
  return error === null ? name : `${name} · erro de ${Math.round(error * 100)}% nas últimas 2 semanas`;
}

export function ForecastPanel({ stores, storeId }: { stores: { id: string; name: string }[]; storeId: string }) {
  const [selected, setSelected] = useState(storeId);
  const [state, setState] = useState<State | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { setSelected(storeId || stores[0]?.id || ""); }, [storeId, stores]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const query = selected ? `?store=${encodeURIComponent(selected)}` : "";
      const response = await fetch(`/api/panel/forecast${query}`, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível calcular a previsão.");
      setState(body);
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Previsão indisponível");
    } finally { setLoading(false); }
  }, [selected]);

  useEffect(() => { void load(); }, [load]);

  const forecast = state?.forecast;
  const young = forecast && forecast.openDays < MIN_OPEN_DAYS;

  return <section className="forecast" aria-label="Previsão da semana">
    <div className="fiscal-heading">
      <div>
        <p className="section-label">PREVISÃO</p>
        <h2>A próxima semana, pelo passado da loja</h2>
        <p>Quanto deve sair de cada produto e de cada insumo, e o que comprar para não faltar.</p>
      </div>
      {stores.length > 1 && (
        <Select id="forecast-store" labelText="Loja" value={selected} onChange={(event) => setSelected(event.target.value)}>
          {stores.map((store) => <SelectItem key={store.id} value={store.id} text={store.name} />)}
        </Select>
      )}
    </div>
    {loading && !state ? <InlineLoading description="Calculando a previsão" /> : null}
    {forecast && young && (
      <p className="forecast-warning">
        {forecast.openDays === 0
          ? "Ainda não há vendas sincronizadas desta loja."
          : `Esta loja tem ${forecast.openDays} dia(s) de vendas. Com menos de ${MIN_OPEN_DAYS}, a previsão é só a média recente — confira antes de comprar.`}
      </p>
    )}
    {forecast && <div className="fiscal-grid forecast-grid">
      <article className="panel">
        <Title icon={<ChartLineData size={18} />} title="Produtos" subtitle={`${forecast.days.length} dias a partir de hoje`} />
        <div className="rows">{forecast.products.length ? forecast.products.map((product) => (
          <ProductRow key={product.key} product={product} />
        )) : <p className="empty">Nenhum produto vendido nas últimas 4 semanas.</p>}</div>
      </article>
      <article className="panel">
        <Title icon={<Box size={18} />} title="Insumos" subtitle="Consumo previsto e compra sugerida" />
        <div className="rows">{forecast.ingredients.length ? forecast.ingredients.map((item) => (
          <IngredientRow key={item.id} item={item} storeId={state!.store!.id} canCount={state!.can_count} onCounted={load} />
        )) : <p className="empty">Nenhuma baixa de insumo sincronizada. Ela chega quando o produto vendido tem receita no caixa.</p>}</div>
      </article>
    </div>}
    {forecast && <p className="forecast-footnote">
      Sazonal é a média do mesmo dia da semana nas últimas 4 semanas em que a loja abriu. O boosting só
      é usado quando errou pelo menos 5% menos que ele nas duas últimas semanas desta loja. A margem de
      segurança cobre o erro da própria previsão com cerca de 90% de confiança.
    </p>}
  </section>;
}

function ProductRow({ product }: { product: Product }) {
  const peak = Math.max(...product.days.map((d) => d.value), 0.0001);
  return <div className="data-row forecast-row">
    <div>
      <strong>{product.name}</strong>
      <small>{methodNote(product.method, product.error)}</small>
    </div>
    <div className="forecast-bars" role="img" aria-label={product.days.map((d) => `${WEEKDAY_NAME[weekday(d.day)]}: ${amount(d.value, product.unit)}`).join(", ")}>
      {product.days.map((d) => (
        <span key={d.day} title={`${WEEKDAY_NAME[weekday(d.day)]} ${d.day.slice(8, 10)}/${d.day.slice(5, 7)}: ${amount(d.value, product.unit)}`}>
          <i style={{ height: `${Math.max(2, (d.value / peak) * 28)}px` }} className={d.value === 0 ? "closed" : ""} />
          <b>{WEEKDAY[weekday(d.day)]}</b>
        </span>
      ))}
    </div>
    <strong className="row-value forecast-total">{amount(product.total, product.unit)}</strong>
  </div>;
}

function IngredientRow({ item, storeId, canCount, onCounted }: {
  item: Ingredient; storeId: string; canCount: boolean; onCounted: () => Promise<void>;
}) {
  const [counting, setCounting] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  async function save(event: FormEvent) {
    event.preventDefault();
    const counted = parseKg(value);
    if (counted === null) return;
    setBusy(true);
    try {
      const response = await fetch("/api/panel/forecast/counts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          store_id: storeId, inventory_item_id: item.id,
          inventory_item_name: item.name, counted_kg: counted,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível lançar a contagem.");
      setCounting(false);
      setValue("");
      await toast(`Contagem de ${item.name} lançada`);
      await onCounted();
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Contagem não lançada");
    } finally { setBusy(false); }
  }

  const balance = item.balanceKg === null
    ? "saldo não contado"
    : `saldo ${kg.format(Math.max(0, item.balanceKg))} kg${item.countedAt ? ` (contado ${new Date(item.countedAt).toLocaleDateString("pt-BR")})` : ""}`;

  return <div className="data-row forecast-row forecast-ingredient">
    <div>
      <strong>{item.name}</strong>
      <small>Consumo previsto {kg.format(item.needKg)} kg · {balance}</small>
      <small>{methodNote(item.method, item.error)}</small>
      {counting && (
        <form className="forecast-count" onSubmit={save}>
          <TextInput id={`count-${item.id}`} labelText="Quanto há agora (kg)" placeholder="12,5"
            inputMode="decimal" autoComplete="off" value={value} onChange={(event) => setValue(event.target.value)}
            invalid={value !== "" && parseKg(value) === null} invalidText="Use um número, como 12,5." />
          <Button type="submit" size="sm" renderIcon={Checkmark} disabled={busy || parseKg(value) === null}>Lançar</Button>
          <Button kind="ghost" size="sm" onClick={() => setCounting(false)}>Cancelar</Button>
        </form>
      )}
    </div>
    <div className="row-end">
      {item.buyKg === null
        ? <span className="state stale">Conte o estoque</span>
        : item.buyKg > 0
          ? <strong className="row-value forecast-buy">Comprar {kg.format(item.buyKg)} kg</strong>
          : <span className="state online">Estoque cobre a semana</span>}
      {item.coverageDays !== null && <small>cobre {units.format(item.coverageDays)} dia(s)</small>}
      {canCount && !counting && <Button kind="ghost" size="sm" onClick={() => setCounting(true)}>Lançar contagem</Button>}
    </div>
  </div>;
}

function Title({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) {
  return <header className="panel-title"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header>;
}
