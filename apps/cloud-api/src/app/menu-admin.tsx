"use client";

/**
 * Cardápio QR no painel — os links impressos nas mesas e o que o cliente vê.
 *
 * A tela começa pelos links porque é o que o dono veio buscar: "onde está o QR
 * da mesa 5?". Produtos vêm depois, e o preço não se edita aqui — ele é do
 * cadastro do caixa e da nota; um segundo lugar para mudá-lo faria o cardápio
 * dizer um valor e o cupom cobrar outro.
 */

import { Button, InlineLoading, Select, SelectItem, TextInput, Toggle } from "@carbon/react";
import { Add, Launch, Printer, QrCode, Restaurant, Save, TrashCan } from "@carbon/icons-react";
import { FormEvent, useCallback, useEffect, useState, type ReactNode } from "react";

import { confirm, fail, toast } from "./alerts";

type Link = {
  id: string; store_id: string; store_name: string; table_label: string | null;
  url: string; created_at: string; revoked_at: string | null;
};
type Product = {
  id: string; sku: string; name: string; price_cents: string; pricing_mode: string;
  category: string | null; description: string | null; menu_visible: boolean; is_active: boolean;
};
type MenuState = { links: Link[]; products: Product[]; can_edit: boolean };

const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });

export function MenuAdmin({ stores }: { stores: { id: string; name: string }[] }) {
  const [state, setState] = useState<MenuState | null>(null);
  const [storeId, setStoreId] = useState("");
  const [table, setTable] = useState("");
  const [busy, setBusy] = useState(false);
  const [qr, setQr] = useState<Link | null>(null);

  const load = useCallback(async () => {
    const response = await fetch("/api/panel/menu", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Não foi possível carregar o cardápio.");
    setState(body);
  }, []);

  useEffect(() => { void load().catch((reason) => fail(message(reason), "Cardápio indisponível")); }, [load]);
  useEffect(() => { if (!storeId && stores[0]) setStoreId(stores[0].id); }, [stores, storeId]);

  async function createLink(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await fetch("/api/panel/menu", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: storeId, table_label: table || undefined }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível criar o link.");
      setTable("");
      await toast(table ? `QR da ${table} criado` : "QR da loja criado");
      await load();
    } catch (reason) {
      await fail(message(reason), "Link não criado");
    } finally { setBusy(false); }
  }

  async function revoke(link: Link) {
    const ok = await confirm({
      title: "Revogar este QR?",
      text: "O QR já impresso deixa de abrir o cardápio. Para voltar, será preciso imprimir um novo.",
      ok: "Revogar",
      danger: true,
    });
    if (!ok) return;
    const response = await fetch(`/api/panel/menu?id=${link.id}`, { method: "DELETE" });
    const body = await response.json();
    if (!response.ok) { await fail(body.detail || "Não foi possível revogar.", "Link mantido"); return; }
    await toast("QR revogado");
    await load();
  }

  if (!state) return <section className="menu-admin"><InlineLoading description="Carregando o cardápio" /></section>;
  const active = state.links.filter((link) => !link.revoked_at);
  const revoked = state.links.filter((link) => link.revoked_at);

  return <section className="menu-admin" aria-label="Cardápio QR">
    <div className="fiscal-heading">
      <div>
        <p className="section-label">CARDÁPIO QR</p>
        <h2>O cardápio na mesa do cliente</h2>
        <p>Cada QR abre o cardápio da loja, com sugestões tiradas das vendas dela. Pedido continua com o garçom.</p>
      </div>
    </div>
    <div className="fiscal-grid">
      <article className="panel">
        <MenuTitle icon={<Add size={18} />} title="Novo QR" subtitle="Um para a loja inteira, ou um por mesa" />
        {state.can_edit ? (
          <form className="owner-form" onSubmit={createLink}>
            <Select id="menu-store" labelText="Loja" value={storeId} onChange={(event) => setStoreId(event.target.value)}>
              {stores.map((store) => <SelectItem key={store.id} value={store.id} text={store.name} />)}
            </Select>
            <TextInput id="menu-table" labelText="Mesa (opcional)" placeholder="Mesa 5" value={table} maxLength={40} onChange={(event) => setTable(event.target.value)} />
            <Button type="submit" renderIcon={QrCode} disabled={busy || !storeId}>{busy ? "Criando" : "Criar QR"}</Button>
            <small>O endereço do QR é aleatório: trocar um caractere não abre o cardápio de outra loja.</small>
          </form>
        ) : <p className="empty">Seu perfil pode ver e imprimir os QR, mas não criar.</p>}
      </article>
      <article className="panel">
        <MenuTitle icon={<QrCode size={18} />} title="QR em uso" subtitle={`${active.length} ativo(s)`} />
        <div className="rows">{active.length ? active.map((link) => (
          <div className="data-row" key={link.id}>
            <div><strong>{link.table_label ?? "Loja inteira"}</strong><small>{link.store_name}</small></div>
            <div className="menu-actions">
              <Button kind="ghost" size="sm" renderIcon={Printer} onClick={() => setQr(link)}>QR</Button>
              <Button kind="ghost" size="sm" renderIcon={Launch} href={link.url} target="_blank" rel="noreferrer">Abrir</Button>
              {state.can_edit && <Button kind="danger--ghost" size="sm" renderIcon={TrashCan} hasIconOnly iconDescription="Revogar" onClick={() => void revoke(link)} />}
            </div>
          </div>
        )) : <p className="empty">Nenhum QR criado ainda.</p>}
        {revoked.length > 0 && <p className="menu-revoked">{revoked.length} QR revogado(s) no histórico.</p>}
        </div>
      </article>
    </div>
    <MenuProducts products={state.products} canEdit={state.can_edit} onSaved={load} />
    {qr && <QrModal link={qr} onClose={() => setQr(null)} />}
  </section>;
}

function MenuProducts({ products, canEdit, onSaved }: { products: Product[]; canEdit: boolean; onSaved: () => Promise<void> }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState({ category: "", description: "", menu_visible: true });
  const [busy, setBusy] = useState(false);

  function start(product: Product) {
    setEditing(product.id);
    setDraft({ category: product.category ?? "", description: product.description ?? "", menu_visible: product.menu_visible });
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    try {
      const response = await fetch("/api/panel/menu/products", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product_id: editing, ...draft }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível salvar.");
      setEditing(null);
      await toast("Cardápio atualizado");
      await onSaved();
    } catch (reason) {
      await fail(message(reason), "Produto não alterado");
    } finally { setBusy(false); }
  }

  const visible = products.filter((product) => product.is_active);
  return <article className="panel menu-products">
    <MenuTitle icon={<Restaurant size={18} />} title="Produtos no cardápio" subtitle="Categoria e descrição que o cliente lê. O preço vem do cadastro." />
    <div className="rows">{visible.length ? visible.map((product) => (
      <div className="data-row menu-product-row" key={product.id}>
        {editing === product.id ? (
          <form className="menu-edit" onSubmit={save}>
            <strong>{product.name}</strong>
            <TextInput id={`cat-${product.id}`} labelText="Categoria" placeholder="Bebidas" value={draft.category} maxLength={40} onChange={(event) => setDraft({ ...draft, category: event.target.value })} />
            <TextInput id={`desc-${product.id}`} labelText="Descrição" placeholder="Feito na casa, servido quente" value={draft.description} maxLength={280} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
            <Toggle id={`vis-${product.id}`} labelText="Aparece no cardápio" labelA="Oculto" labelB="Visível" toggled={draft.menu_visible} onToggle={(value) => setDraft({ ...draft, menu_visible: value })} />
            <div className="menu-actions">
              <Button type="submit" size="sm" renderIcon={Save} disabled={busy}>Salvar</Button>
              <Button kind="ghost" size="sm" onClick={() => setEditing(null)}>Cancelar</Button>
            </div>
          </form>
        ) : (
          <>
            <div>
              <strong>{product.name}</strong>
              <small>{product.category ?? "Sem categoria"}{product.menu_visible ? "" : " · oculto"}{product.description ? ` · ${product.description}` : ""}</small>
            </div>
            <div className="row-end">
              <strong className="row-value">{money.format(Number(product.price_cents) / 100)}{product.pricing_mode === "weight" ? "/kg" : ""}</strong>
              {canEdit && <Button kind="ghost" size="sm" onClick={() => start(product)}>Editar</Button>}
            </div>
          </>
        )}
      </div>
    )) : <p className="empty">Os produtos chegam aqui pela sincronização do caixa ou pelo cadastro.</p>}</div>
  </article>;
}

function QrModal({ link, onClose }: { link: Link; onClose: () => void }) {
  return <div className="menu-qr-backdrop" role="dialog" aria-modal="true" aria-label="QR do cardápio" onClick={onClose}>
    <div className="menu-qr-card" onClick={(event) => event.stopPropagation()}>
      <p className="section-label">{link.store_name}</p>
      <h3>{link.table_label ?? "Cardápio"}</h3>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`/api/panel/menu/qr?id=${link.id}`} alt={`QR do cardápio ${link.table_label ?? link.store_name}`} width={280} height={280} />
      <small>Aponte a câmera do celular para abrir o cardápio</small>
      <code>{link.url}</code>
      <div className="menu-actions">
        <Button size="sm" renderIcon={Printer} onClick={() => window.print()}>Imprimir</Button>
        <Button kind="secondary" size="sm" onClick={onClose}>Fechar</Button>
      </div>
    </div>
  </div>;
}

function MenuTitle({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) {
  return <header className="panel-title"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header>;
}

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : "Falha de rede.";
}
