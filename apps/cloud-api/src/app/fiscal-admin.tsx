"use client";

/**
 * Cadastro fiscal do dono — a loja emitente e o perfil tributário de cada
 * produto.
 *
 * A tela começa pelo que IMPEDE a primeira nota, e não pelo formulário: o dono
 * que abre esta seção quer saber "o que falta para emitir?", e uma lista de
 * pendências em português responde isso melhor do que vinte campos vazios.
 *
 * O certificado A1 e o CSC não são enviados por aqui. O campo pede o nome da
 * referência no cofre do serviço fiscal, e a própria tela diz isso — um campo
 * de upload "por conveniência" poria o certificado que assina notas em nome da
 * empresa no log do proxy.
 */

import { Button, InlineLoading, Select, SelectItem, Tag, TextInput, Toggle } from "@carbon/react";
import { Checkmark, DocumentTasks, Edit, Receipt, Save, WarningAlt } from "@carbon/icons-react";
import { FormEvent, useCallback, useEffect, useState, type ReactNode } from "react";

import { confirm, fail, toast } from "./alerts";

type Address = { street: string; number: string; district: string; city_code: string; city: string; zip: string };
type Config = {
  uf: string; environment: "homologation" | "production"; cnpj: string; state_registration: string;
  tax_regime: number; legal_name: string; address_json: Address; certificate_ref: string | null;
  csc_ref: string | null; csc_id: string | null; enabled: boolean;
};
type Product = {
  id: string; sku: string; name: string; price_cents: string; ncm: string | null; cfop: string | null;
  cest: string | null; unit_code: string | null; origin: number | null; csosn: string | null;
  cst_icms: string | null; cst_pis: string | null; cst_cofins: string | null;
  complete: boolean; problems: string[];
};
type FiscalState = {
  config: Config | null; series: { series: number; next_number: number } | null;
  products: Product[]; blockers: string[]; production_enabled: boolean;
};

const EMPTY_ADDRESS: Address = { street: "", number: "", district: "", city_code: "", city: "", zip: "" };
const REGIMES = [
  { value: 1, label: "1 — Simples Nacional" },
  { value: 2, label: "2 — Simples, excesso de sublimite" },
  { value: 3, label: "3 — Regime Normal" },
  { value: 4, label: "4 — MEI" },
];
const ORIGINS = [
  "0 — Nacional", "1 — Estrangeira, importação direta", "2 — Estrangeira, mercado interno",
  "3 — Nacional, conteúdo importado > 40%", "4 — Nacional, processos produtivos básicos",
  "5 — Nacional, conteúdo importado ≤ 40%", "6 — Estrangeira, importação direta sem similar",
  "7 — Estrangeira, mercado interno sem similar", "8 — Nacional, conteúdo importado > 70%",
];
const usesCsosn = (regime: number) => regime === 1 || regime === 4;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `Erro ${response.status}`);
  return body as T;
}

export function FiscalAdmin({ stores }: { stores: { id: string; name: string }[] }) {
  const [storeId, setStoreId] = useState(stores[0]?.id ?? "");
  const [state, setState] = useState<FiscalState | null>(null);
  const [editing, setEditing] = useState<Product | null>(null);

  const load = useCallback(async () => {
    if (!storeId) return;
    try {
      setState(await request<FiscalState>(`/api/panel/fiscal?store=${storeId}`));
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Cadastro fiscal indisponível");
    }
  }, [storeId]);

  useEffect(() => { if (!storeId && stores[0]) setStoreId(stores[0].id); }, [stores, storeId]);
  useEffect(() => { void load(); }, [load]);

  if (!stores.length) return null;

  return <section className="fiscal-admin" aria-label="Cadastro fiscal">
    <div className="fiscal-heading">
      <div><p className="section-label">FISCAL</p><h2>NFC-e da loja</h2>
        <p>O emitente e a tributação de cada produto. O certificado fica no cofre do serviço fiscal.</p></div>
      <Select id="fiscal-store" labelText="Loja" value={storeId} onChange={(event) => { setEditing(null); setStoreId(event.target.value); }}>
        {stores.map((store) => <SelectItem key={store.id} value={store.id} text={store.name} />)}
      </Select>
    </div>

    {!state ? <InlineLoading description="Carregando cadastro fiscal" /> : <>
      <Readiness blockers={state.blockers} />
      <div className="fiscal-grid">
        <ConfigForm key={storeId} storeId={storeId} state={state} onSaved={load} />
        <article className="panel">
          <PanelHeader icon={<DocumentTasks size={18} />} title="Perfil tributário dos produtos"
            subtitle="NCM, CFOP e CST definidos pelo contador — o sistema confere, não sugere" />
          <div className="rows">{state.products.length ? state.products.map((product) =>
            <div className="data-row" key={product.id}>
              <div><strong>{product.name}</strong>
                <small>{product.complete ? `NCM ${product.ncm} · CFOP ${product.cfop}` : product.problems[0]}</small></div>
              <div className="row-end fiscal-row-end">
                <Tag type={product.complete ? "green" : "warm-gray"} size="sm">{product.complete ? "completo" : "pendente"}</Tag>
                <Button kind="ghost" size="sm" renderIcon={Edit} iconDescription={`Editar ${product.name}`} hasIconOnly onClick={() => setEditing(product)} />
              </div>
            </div>) : <div className="empty">Nenhum produto ativo no catálogo.</div>}</div>
          {editing && <ProductForm key={editing.id} product={editing}
            regime={state.config?.tax_regime ?? null}
            onClose={() => setEditing(null)}
            onSaved={async () => { setEditing(null); await load(); }} />}
        </article>
      </div>
    </>}
  </section>;
}

function Readiness({ blockers }: { blockers: string[] }) {
  if (!blockers.length) {
    return <div className="fiscal-ready"><Checkmark size={16} />Pronto para emitir NFC-e nesta loja.</div>;
  }
  return <div className="fiscal-blockers" role="status">
    <strong><WarningAlt size={16} />O que ainda impede a primeira nota</strong>
    <ul>{blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
  </div>;
}

function ConfigForm({ storeId, state, onSaved }: { storeId: string; state: FiscalState; onSaved: () => Promise<void> }) {
  const current = state.config;
  const [form, setForm] = useState({
    legal_name: current?.legal_name ?? "", cnpj: current?.cnpj ?? "",
    state_registration: current?.state_registration ?? "", tax_regime: current?.tax_regime ?? 1,
    uf: current?.uf ?? "RJ", environment: current?.environment ?? "homologation",
    certificate_ref: current?.certificate_ref ?? "", csc_ref: current?.csc_ref ?? "",
    csc_id: current?.csc_id ?? "", enabled: current?.enabled ?? false,
    normal_series: String(state.series?.series ?? 1),
  });
  const [address, setAddress] = useState<Address>({ ...EMPTY_ADDRESS, ...(current?.address_json ?? {}) });
  const [busy, setBusy] = useState(false);
  const seriesLocked = Boolean(state.series && state.series.next_number > 1);

  const field = (name: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((previous) => ({ ...previous, [name]: event.target.value }));
  const addressField = (name: keyof Address) => (event: { target: { value: string } }) =>
    setAddress((previous) => ({ ...previous, [name]: event.target.value }));

  async function save(event: FormEvent) {
    event.preventDefault();
    // Ligar a emissão e ir para produção têm consequência fiscal real; os dois
    // pedem um segundo gesto.
    if (form.enabled && !current?.enabled && !await confirm({
      title: "Ligar a emissão de NFC-e?",
      text: "A partir de agora, toda venda paga desta loja pede nota ao serviço fiscal. Vendas com total zero (desconto de 100%) continuam sem nota.",
      ok: "Ligar emissão",
    })) return;
    if (form.environment === "production" && current?.environment !== "production" && !await confirm({
      title: "Emitir em PRODUÇÃO?",
      text: "As notas passam a ter valor fiscal e a consumir a numeração real da série. Só confirme depois da homologação.",
      ok: "Ir para produção", danger: true,
    })) return;

    setBusy(true);
    try {
      await request("/api/panel/fiscal", {
        method: "PUT",
        body: JSON.stringify({
          ...form, store_id: storeId, tax_regime: Number(form.tax_regime),
          normal_series: Number(form.normal_series), address,
        }),
      });
      await toast("Cadastro fiscal salvo");
      await onSaved();
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Cadastro não salvo");
    } finally { setBusy(false); }
  }

  return <article className="panel">
    <PanelHeader icon={<Receipt size={18} />} title="Emitente" subtitle="Os dados que saem no cabeçalho de toda nota" />
    <form className="fiscal-form" onSubmit={save}>
      <TextInput id="f-legal" labelText="Razão social" maxLength={60} value={form.legal_name} onChange={field("legal_name")} required />
      <div className="form-pair">
        <TextInput id="f-cnpj" labelText="CNPJ" value={form.cnpj} onChange={field("cnpj")} required />
        <TextInput id="f-ie" labelText="Inscrição estadual" value={form.state_registration} onChange={field("state_registration")} required />
      </div>
      <div className="form-pair">
        <Select id="f-crt" labelText="Regime tributário (CRT)" value={String(form.tax_regime)} onChange={field("tax_regime")}>
          {REGIMES.map((regime) => <SelectItem key={regime.value} value={String(regime.value)} text={regime.label} />)}
        </Select>
        <TextInput id="f-uf" labelText="UF" maxLength={2} value={form.uf} onChange={(event) => setForm((p) => ({ ...p, uf: event.target.value.toUpperCase() }))} required />
      </div>
      <TextInput id="f-street" labelText="Logradouro" maxLength={60} value={address.street} onChange={addressField("street")} required />
      <div className="form-pair">
        <TextInput id="f-number" labelText="Número" value={address.number} onChange={addressField("number")} required />
        <TextInput id="f-district" labelText="Bairro" value={address.district} onChange={addressField("district")} required />
      </div>
      <div className="form-pair">
        <TextInput id="f-city" labelText="Município" value={address.city} onChange={addressField("city")} required />
        <TextInput id="f-ibge" labelText="Código IBGE do município" helperText="7 dígitos (Rio de Janeiro: 3304557)" value={address.city_code} onChange={addressField("city_code")} required />
      </div>
      <TextInput id="f-zip" labelText="CEP" value={address.zip} onChange={addressField("zip")} required />

      <div className="form-pair">
        <Select id="f-env" labelText="Ambiente SEFAZ" value={form.environment} onChange={field("environment")}>
          <SelectItem value="homologation" text="Homologação (sem valor fiscal)" />
          <SelectItem value="production" text="Produção" disabled={!state.production_enabled} />
        </Select>
        <TextInput id="f-series" labelText="Série normal" type="number" min={1} max={999}
          helperText={seriesLocked ? "Já emitiu documentos; não pode ser trocada." : "Números de 1 a 999."}
          value={form.normal_series} disabled={seriesLocked} onChange={field("normal_series")} />
      </div>

      <TextInput id="f-a1" labelText="Referência do certificado A1 no cofre"
        helperText="O NOME do arquivo no cofre do serviço fiscal (ex.: loja-centro/a1.pfx). Nunca o arquivo nem a senha."
        value={form.certificate_ref} onChange={field("certificate_ref")} />
      <div className="form-pair">
        <TextInput id="f-csc" labelText="Referência do CSC no cofre" value={form.csc_ref} onChange={field("csc_ref")} />
        <TextInput id="f-cscid" labelText="ID do CSC" helperText="Fornecido pela SEFAZ junto do CSC." value={form.csc_id} onChange={field("csc_id")} />
      </div>

      <Toggle id="f-enabled" labelText="Emissão de NFC-e" labelA="Desligada" labelB="Ligada"
        toggled={form.enabled} onToggle={(value: boolean) => setForm((p) => ({ ...p, enabled: value }))} />
      <Button type="submit" renderIcon={Save} disabled={busy}>{busy ? "Salvando" : "Salvar cadastro"}</Button>
      <small>Com a emissão desligada, o cadastro pode ser salvo sem as referências do certificado — para ligar, tudo precisa estar completo.</small>
    </form>
  </article>;
}

function ProductForm({ product, regime, onClose, onSaved }: {
  product: Product; regime: number | null; onClose: () => void; onSaved: () => Promise<void>;
}) {
  const simples = regime === null ? Boolean(product.csosn) || !product.cst_icms : usesCsosn(regime);
  const [form, setForm] = useState({
    ncm: product.ncm ?? "", cfop: product.cfop ?? "5102", cest: product.cest ?? "",
    unit_code: product.unit_code ?? "UN", origin: String(product.origin ?? 0),
    icms: (simples ? product.csosn : product.cst_icms) ?? "",
    cst_pis: product.cst_pis ?? "", cst_cofins: product.cst_cofins ?? "",
  });
  const [busy, setBusy] = useState(false);
  const field = (name: keyof typeof form) => (event: { target: { value: string } }) =>
    setForm((previous) => ({ ...previous, [name]: event.target.value }));

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await request("/api/panel/fiscal/products", {
        method: "PUT",
        body: JSON.stringify({
          product_id: product.id, ncm: form.ncm, cfop: form.cfop, cest: form.cest || null,
          unit_code: form.unit_code, origin: Number(form.origin),
          csosn: simples ? form.icms : null, cst_icms: simples ? null : form.icms,
          cst_pis: form.cst_pis, cst_cofins: form.cst_cofins,
        }),
      });
      await toast(`${product.name}: perfil salvo`);
      await onSaved();
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Perfil não salvo");
    } finally { setBusy(false); }
  }

  return <form className="fiscal-form product-form" onSubmit={save}>
    <strong>{product.name}</strong>
    <div className="form-pair">
      <TextInput id="p-ncm" labelText="NCM" helperText="8 dígitos" value={form.ncm} onChange={field("ncm")} required />
      <TextInput id="p-cfop" labelText="CFOP" helperText="Operação interna: 5xxx" value={form.cfop} onChange={field("cfop")} required />
    </div>
    <div className="form-pair">
      <TextInput id="p-cest" labelText="CEST (se houver)" value={form.cest} onChange={field("cest")} />
      <TextInput id="p-unit" labelText="Unidade" helperText="UN, KG…" maxLength={6} value={form.unit_code} onChange={field("unit_code")} required />
    </div>
    <Select id="p-origin" labelText="Origem da mercadoria" value={form.origin} onChange={field("origin")}>
      {ORIGINS.map((label, index) => <SelectItem key={label} value={String(index)} text={label} />)}
    </Select>
    <div className="form-pair">
      <TextInput id="p-icms" labelText={simples ? "CSOSN (Simples/MEI)" : "CST de ICMS"} value={form.icms} onChange={field("icms")} required />
      <TextInput id="p-pis" labelText="CST de PIS" value={form.cst_pis} onChange={field("cst_pis")} required />
    </div>
    <TextInput id="p-cofins" labelText="CST de COFINS" value={form.cst_cofins} onChange={field("cst_cofins")} required />
    <div className="form-actions">
      <Button kind="secondary" onClick={onClose} disabled={busy}>Cancelar</Button>
      <Button type="submit" renderIcon={Save} disabled={busy}>{busy ? "Salvando" : "Salvar perfil"}</Button>
    </div>
  </form>;
}

function PanelHeader({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) {
  return <header className="panel-title"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header>;
}
