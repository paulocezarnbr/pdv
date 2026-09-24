/**
 * O que o painel diz sobre um terminal — puro, para ser testado sem tela.
 *
 * A ordem das mensagens é a ordem de gravidade: venda presa primeiro, depois
 * quarentena, depois relógio. O dono lê a primeira linha e, muitas vezes, só
 * ela.
 */

export interface DeviceTelemetry {
  reported_at: string | null;
  pending_items: number | null;
  quarantined_items: number | null;
  oldest_pending_at: string | null;
  clock_drift_ms: string | null;
  last_quarantine_reason: string | null;
  queue_stuck: boolean;
  clock_skewed: boolean;
}

export interface Issue {
  tone: "danger" | "warning";
  text: string;
}

export function ago(value: string, now: number): string {
  const seconds = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.floor(hours / 24)} dias`;
}

/** "+3 min", "-40 s": o sinal diz se o caixa está adiantado ou atrasado. */
export function formatDrift(ms: number): string {
  const sign = ms >= 0 ? "+" : "-";
  const seconds = Math.round(Math.abs(ms) / 1000);
  if (seconds < 120) return `${sign}${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 120) return `${sign}${minutes} min`;
  return `${sign}${Math.round(minutes / 60)} h`;
}

export function deviceIssues(device: DeviceTelemetry, now = Date.now()): Issue[] {
  if (!device.reported_at) {
    // Terminal antigo, ou que nunca completou um ciclo: não inventa "tudo bem".
    return [{ tone: "warning", text: "sem relato de fila ainda" }];
  }

  const issues: Issue[] = [];
  const pending = device.pending_items ?? 0;
  if (device.queue_stuck && device.oldest_pending_at) {
    issues.push({
      tone: "danger",
      text: `${pending} venda(s) presa(s) na fila há ${ago(device.oldest_pending_at, now)}`,
    });
  }
  const quarantined = device.quarantined_items ?? 0;
  if (quarantined > 0) {
    const reason = device.last_quarantine_reason
      ? `: ${device.last_quarantine_reason.slice(0, 120)}`
      : "";
    issues.push({ tone: "danger", text: `${quarantined} em quarentena${reason}` });
  }
  if (device.clock_skewed && device.clock_drift_ms !== null) {
    issues.push({
      tone: "warning",
      text: `relógio do caixa ${formatDrift(Number(device.clock_drift_ms))}`,
    });
  }
  return issues;
}

/** A linha discreta de quando está tudo bem: fila e frescor do relato. */
export function queueSummary(device: DeviceTelemetry, now = Date.now()): string {
  if (!device.reported_at) return "";
  const pending = device.pending_items ?? 0;
  const queue = pending === 0 ? "fila vazia" : `${pending} na fila`;
  return `${queue} · relato há ${ago(device.reported_at, now)}`;
}
