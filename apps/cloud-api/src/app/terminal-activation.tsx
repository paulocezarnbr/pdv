"use client";

/**
 * Ativar um caixa — o código que o PDV pede na primeira abertura.
 *
 * O código aparece uma vez e vale 15 minutos. A tela mostra também o endereço
 * que o técnico digita no PDV, porque é o outro campo da mesma tela de
 * ativação e o que mais se erra ao telefone.
 */

import { Button, Select, SelectItem, TextInput } from "@carbon/react";
import { Add, Copy, Devices } from "@carbon/icons-react";
import { FormEvent, useEffect, useState } from "react";

import { fail, toast } from "./alerts";

type Issued = { code: string; label: string; store_name: string; expires_at: string; ttl_minutes: number };

export function TerminalActivation({ stores }: { stores: { id: string; name: string }[] }) {
  const [storeId, setStoreId] = useState("");
  const [label, setLabel] = useState("Caixa 1");
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [server, setServer] = useState("");

  useEffect(() => { setServer(window.location.host); }, []);
  useEffect(() => { if (!storeId && stores[0]) setStoreId(stores[0].id); }, [stores, storeId]);

  async function issue(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await fetch("/api/panel/devices/activation-codes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: storeId, label }),
        cache: "no-store",
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível gerar o código.");
      setIssued(body);
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Código não gerado");
    } finally { setBusy(false); }
  }

  async function copy() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.code);
      await toast("Código copiado");
    } catch {
      await fail("O navegador não deixou copiar. Selecione o código e copie à mão.", "Cópia bloqueada");
    }
  }

  const until = issued ? new Date(issued.expires_at).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "";

  return <section className="owner-admin" aria-label="Ativar terminal">
    <article className="panel owner-form-panel">
      <header className="panel-title"><span><Add size={18} /></span><div><h2>Ativar um caixa</h2><p>Gere o código que o PDV pede na primeira abertura</p></div></header>
      <form className="owner-form" onSubmit={issue}>
        <Select id="activation-store" labelText="Loja" value={storeId} onChange={(event) => setStoreId(event.target.value)} required>
          {stores.map((store) => <SelectItem key={store.id} value={store.id} text={store.name} />)}
        </Select>
        <TextInput id="activation-label" labelText="Nome do terminal" helperText="Como ele aparece no painel: Caixa 1, Balcão, Salão." value={label} maxLength={60} onChange={(event) => setLabel(event.target.value)} required />
        <Button type="submit" renderIcon={Devices} disabled={busy || !storeId}>{busy ? "Gerando" : "Gerar código de ativação"}</Button>
        <small>O código vale 15 minutos e ativa um único terminal. O painel guarda só o hash dele.</small>
      </form>
    </article>
    <article className="panel">
      <header className="panel-title"><span><Devices size={18} /></span><div><h2>No PDV</h2><p>Na tela “Ativar terminal”, preencha os dois campos</p></div></header>
      {issued ? <div className="activation-result">
        <small>Endereço da retaguarda</small>
        <strong className="activation-server">{server}</strong>
        <small>Código de ativação — {issued.label}, {issued.store_name}</small>
        <div className="activation-code-row">
          <strong className="activation-code" aria-label="Código de ativação">{issued.code}</strong>
          <Button kind="ghost" size="sm" renderIcon={Copy} iconDescription="Copiar código" hasIconOnly onClick={copy} />
        </div>
        <small>Vale até {until}. Depois disso, ou se já tiver sido usado, gere outro.</small>
      </div> : <div className="empty">Gere um código para ver aqui o que digitar no caixa.</div>}
    </article>
  </section>;
}
