"use client";

import { Button, InlineLoading, InlineNotification, Select, SelectItem, TextInput } from "@carbon/react";
import { Add, ChartLine, Logout, Renew, Restaurant, Security, Store, UserMultiple, WarningAlt } from "@carbon/icons-react";
import { FormEvent, useCallback, useEffect, useState, type ReactNode } from "react";

import { fail, toast } from "./alerts";
import { FiscalAdmin } from "./fiscal-admin";
import { deviceIssues, queueSummary, type DeviceTelemetry } from "@/lib/device-health";

type SessionUser = { name: string; email: string; role: string };
type Summary = { revenue_cents: string; tips_cents: string; discount_cents: string; orders_count: string; avg_ticket_cents: string; cmv_cents: string };
type Dashboard = {
  generatedAt: string;
  stores: { id: string; name: string }[];
  summary: Summary;
  hourly: { bucket: string; revenue_cents: string; orders_count: string }[];
  products: { name: string; quantity: string; revenue_cents: string }[];
  staff: { id: string; name: string; orders_count: string; revenue_cents: string; tips_cents: string }[];
  devices: ({ id: string; label: string; store_name: string; last_seen_at: string | null; pending_commands: string; open_alerts: string } & DeviceTelemetry)[];
  alerts: { id: string; reason: string; store_name: string | null; device_id: string; raised_at: string }[];
};
type Owner = { id: string; name: string; login: string; is_active: boolean; updated_at: string };

const money = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const integer = new Intl.NumberFormat("pt-BR");
const cents = (value: string | number) => money.format(Number(value) / 100);

function since(value: string | null, now = Date.now()) {
  if (!value) return { label: "nunca sincronizou", stale: true };
  const seconds = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000));
  if (seconds < 60) return { label: `há ${seconds}s`, stale: false };
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return { label: `há ${minutes} min`, stale: minutes >= 5 };
  return { label: `há ${Math.floor(minutes / 60)} h`, stale: true };
}

function rangeFrom(preset: string) {
  const to = new Date();
  const from = new Date(to);
  if (preset === "today") from.setHours(0, 0, 0, 0);
  else from.setDate(from.getDate() - Number(preset));
  return { from: from.toISOString(), to: to.toISOString() };
}

export function DashboardApp() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [storeId, setStoreId] = useState("");
  const [preset, setPreset] = useState("today");

  useEffect(() => {
    fetch("/api/panel/session", { credentials: "same-origin" })
      .then(async (response) => response.ok ? (await response.json()).user as SessionUser : null)
      .then(setUser).finally(() => setChecking(false));
  }, []);

  const refresh = useCallback(async () => {
    if (!user) return;
    setLoading(true); setError("");
    const params = new URLSearchParams(rangeFrom(preset));
    if (storeId) params.set("store", storeId);
    try {
      const response = await fetch(`/api/panel/dashboard?${params}`, { cache: "no-store" });
      const body = await response.json();
      if (response.status === 401) { setUser(null); setData(null); return; }
      if (!response.ok) throw new Error(body.detail || "Não foi possível atualizar o painel.");
      setData(body);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Falha de rede.");
    } finally { setLoading(false); }
  }, [preset, storeId, user]);

  useEffect(() => {
    if (!user) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [refresh, user]);

  if (checking) return <div className="center-state"><InlineLoading description="Verificando sessão" /></div>;
  if (!user) return <Login onLogin={setUser} />;

  const freshness = data ? since(data.generatedAt) : null;
  const summary = data?.summary;
  const revenue = Number(summary?.revenue_cents ?? 0);
  const cmv = Number(summary?.cmv_cents ?? 0);
  const margin = revenue > 0 ? ((revenue - cmv) / revenue) * 100 : 0;
  const maxHour = Math.max(1, ...(data?.hourly.map((row) => Number(row.revenue_cents)) ?? [1]));

  async function logout() {
    await fetch("/api/panel/session", { method: "DELETE" });
    setUser(null); setData(null);
  }

  return <div className="dashboard-shell">
    <header className="topbar">
      <div className="brand"><Restaurant size={20} /><span>ERP Food</span></div>
      <div className="operator"><span><strong>{user.name}</strong><small>{user.role}</small></span><Button kind="ghost" size="sm" renderIcon={Logout} iconDescription="Sair" hasIconOnly onClick={logout} /></div>
    </header>
    <main className="dashboard-main">
      <section className="page-heading">
        <div><p className="section-label">OPERAÇÃO</p><h1>Visão das lojas</h1><p>Vendas recebidas dos terminais e alertas que pedem ação.</p></div>
        <div className="filters">
          <Select id="period" labelText="Período" value={preset} onChange={(event) => setPreset(event.target.value)}><SelectItem value="today" text="Hoje" /><SelectItem value="7" text="Últimos 7 dias" /><SelectItem value="30" text="Últimos 30 dias" /></Select>
          <Select id="store" labelText="Loja" value={storeId} onChange={(event) => setStoreId(event.target.value)}><SelectItem value="" text="Todas as lojas" />{(data?.stores ?? []).map((store) => <SelectItem key={store.id} value={store.id} text={store.name} />)}</Select>
          <Button kind="tertiary" size="md" renderIcon={Renew} onClick={() => void refresh()} disabled={loading}>Atualizar</Button>
        </div>
      </section>
      {error && <InlineNotification kind="error" title="Painel não atualizado" subtitle={error} lowContrast hideCloseButton />}
      <div className="freshness" aria-live="polite">{loading ? <InlineLoading description="Atualizando" /> : <><span className={freshness?.stale ? "signal danger" : "signal good"} />Dados atualizados {freshness?.label ?? "agora"}</>}</div>
      <section className="metrics" aria-label="Resumo financeiro">
        <Metric label="Faturamento" value={cents(summary?.revenue_cents ?? 0)} note={`${integer.format(Number(summary?.orders_count ?? 0))} pedidos`} />
        <Metric label="Ticket médio" value={cents(summary?.avg_ticket_cents ?? 0)} note={`Descontos ${cents(summary?.discount_cents ?? 0)}`} />
        <Metric label="CMV" value={cents(summary?.cmv_cents ?? 0)} note={`Margem bruta ${margin.toFixed(1).replace(".", ",")}%`} />
        <Metric label="Gorjetas" value={cents(summary?.tips_cents ?? 0)} note="Fora do faturamento" />
      </section>
      <section className="operations-grid">
        <article className="panel hourly-panel"><PanelTitle icon={<ChartLine size={18} />} title="Venda por hora" subtitle="Somente comandas pagas" />{data?.hourly.length ? <div className="hour-chart" aria-label="Gráfico de vendas por hora">{data.hourly.map((row) => { const value = Number(row.revenue_cents); return <div className="hour-column" key={row.bucket} title={`${new Date(row.bucket).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}: ${cents(value)}`}><div className="hour-value">{cents(value)}</div><div className="hour-bar" style={{ height: `${Math.max(4, value / maxHour * 148)}px` }} /><div className="hour-label">{new Date(row.bucket).toLocaleTimeString("pt-BR", { hour: "2-digit" })}</div></div>; })}</div> : <Empty text="Nenhuma venda paga neste período." />}</article>
        <article className="panel"><PanelTitle icon={<Store size={18} />} title="Terminais" subtitle="Frescor, fila e relógio de cada caixa" /><div className="rows">{data?.devices.length ? data.devices.map((device) => { const seen = since(device.last_seen_at); const issues = deviceIssues(device); return <div className="data-row device-row" key={device.id}><div><strong>{device.label}</strong><small>{device.store_name}</small>{issues.map((issue) => <small key={issue.text} className={`device-issue ${issue.tone}`}>{issue.text}</small>)}</div><div className="row-end"><span className={seen.stale ? "state stale" : "state online"}>{seen.label}</span><small>{queueSummary(device) || `${device.pending_commands} comando(s) pendente(s)`}</small></div></div>; }) : <Empty text="Nenhum terminal ativado." />}</div></article>
      </section>
      <section className="triple-grid">
        <article className="panel"><PanelTitle icon={<Restaurant size={18} />} title="Produtos" subtitle="Ranking por faturamento" /><RankRows rows={(data?.products ?? []).map((p) => ({ key: p.name, name: p.name, value: cents(p.revenue_cents), note: `${p.quantity} lançamento(s)` }))} /></article>
        <article className="panel"><PanelTitle icon={<Store size={18} />} title="Equipe" subtitle="Resultado e gorjeta" /><RankRows rows={(data?.staff ?? []).map((s) => ({ key: s.id, name: s.name, value: cents(s.revenue_cents), note: `${s.orders_count} mesa(s), ${cents(s.tips_cents)} em gorjetas` }))} /></article>
        <article className="panel alert-panel"><PanelTitle icon={<Security size={18} />} title="Segurança" subtitle="Alertas antifraude em aberto" /><div className="rows">{data?.alerts.length ? data.alerts.map((alert) => <div className="alert-row" key={alert.id}><WarningAlt size={16} /><div><strong>{alert.reason}</strong><small>{alert.store_name ?? "Loja não identificada"} - {since(alert.raised_at).label}</small></div></div>) : <Empty text="Nenhum alerta em aberto." />}</div></article>
      </section>
      {user.role === "owner" && <><OwnerAdmin /><FiscalAdmin stores={data?.stores ?? []} /></>}
    </main>
  </div>;
}

function OwnerAdmin() {
  const [owners, setOwners] = useState<Owner[]>([]);
  const [name, setName] = useState("");
  const [login, setLogin] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const response = await fetch("/api/panel/owners", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Não foi possível carregar os proprietários.");
    setOwners(body.owners);
  }, []);

  useEffect(() => { void load().catch((reason) => fail(reason instanceof Error ? reason.message : "Falha de rede.", "Proprietários indisponíveis")); }, [load]);

  async function createOwner(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const response = await fetch("/api/panel/owners", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, login, pin }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Não foi possível cadastrar o proprietário.");
      setName(""); setLogin(""); setPin("");
      await toast(`${body.owner.name} cadastrado como Dono`);
      await load();
    } catch (reason) {
      await fail(reason instanceof Error ? reason.message : "Falha de rede.", "Cadastro não concluído");
    } finally { setBusy(false); }
  }

  return <section className="owner-admin" aria-label="Proprietários">
    <article className="panel owner-form-panel">
      <PanelTitle icon={<Add size={18} />} title="Adicionar outro dono" subtitle="Somente um proprietário autenticado pode conceder este poder" />
      <form className="owner-form" onSubmit={createOwner}>
        <TextInput id="owner-name" labelText="Nome completo" value={name} maxLength={120} onChange={(event) => setName(event.target.value)} required />
        <TextInput id="owner-login" labelText="Login no PDV" helperText="3 a 40 caracteres: letras minúsculas, números, ponto, hífen ou sublinhado." value={login} maxLength={40} autoComplete="off" onChange={(event) => setLogin(event.target.value.toLowerCase())} required />
        <TextInput id="owner-pin" labelText="PIN operacional" helperText="6 a 12 dígitos, sem sequências ou repetições previsíveis." type="password" inputMode="numeric" autoComplete="new-password" value={pin} maxLength={12} onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))} required />
        <Button type="submit" renderIcon={Add} disabled={busy}>{busy ? "Protegendo PIN" : "Cadastrar dono"}</Button>
        <small>O PIN é transformado em Argon2id antes de ser gravado. Ele nunca volta para esta tela.</small>
      </form>
    </article>
    <article className="panel">
      <PanelTitle icon={<UserMultiple size={18} />} title="Donos cadastrados" subtitle="Contas operacionais com poder máximo no tenant" />
      <div className="rows owner-list">{owners.length ? owners.map((owner) => <div className="data-row" key={owner.id}><div><strong>{owner.name}</strong><small>@{owner.login}</small></div><span className={owner.is_active ? "state online" : "state stale"}>{owner.is_active ? "ativo" : "inativo"}</span></div>) : <Empty text="Nenhum proprietário operacional cadastrado." />}</div>
    </article>
  </section>;
}

function Login({ onLogin }: { onLogin: (user: SessionUser) => void }) {
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); setBusy(true); setError(""); try { const response = await fetch("/api/panel/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) }); const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Não foi possível entrar."); onLogin(body.user); } catch (reason) { setError(reason instanceof Error ? reason.message : "Falha de rede."); } finally { setBusy(false); } }
  return <main className="login-page"><section className="login-context"><Restaurant size={28} /><div><p className="section-label">ERP FOOD</p><h1>O salão e o caixa, vistos de fora da loja.</h1><p>Dados sincronizados, terminais visíveis e alertas antifraude em uma única retaguarda.</p></div></section><form className="login-form" onSubmit={submit}><div><h2>Entrar no painel</h2><p>Use a conta administrativa do seu tenant.</p></div>{error && <InlineNotification kind="error" title="Acesso não liberado" subtitle={error} lowContrast hideCloseButton />}<TextInput id="email" labelText="E-mail" type="email" value={email} autoComplete="username" onChange={(event) => setEmail(event.target.value)} required /><TextInput id="password" labelText="Senha" type="password" value={password} autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} required /><Button type="submit" disabled={busy}>{busy ? "Verificando" : "Entrar"}</Button><small>O acesso tem limite de tentativas e a sessão expira automaticamente.</small></form></main>;
}

function Metric({ label, value, note }: { label: string; value: string; note: string }) { return <div className="metric"><span>{label}</span><strong>{value}</strong><small>{note}</small></div>; }
function PanelTitle({ icon, title, subtitle }: { icon: ReactNode; title: string; subtitle: string }) { return <header className="panel-title"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header>; }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div>; }
function RankRows({ rows }: { rows: { key: string; name: string; value: string; note: string }[] }) { return <div className="rows">{rows.length ? rows.map((row, index) => <div className="data-row" key={row.key}><span className="rank">{index + 1}</span><div><strong>{row.name}</strong><small>{row.note}</small></div><strong className="row-value">{row.value}</strong></div>) : <Empty text="Sem dados neste período." />}</div>; }
