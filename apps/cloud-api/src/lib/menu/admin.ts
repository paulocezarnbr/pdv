/**
 * Regras do cardápio no painel: quem mexe e onde o link aponta.
 *
 * Criar ou revogar link, e mudar o que o cliente vê, é de dono e gerente.
 * Quem só consulta (`viewer`) vê os links e o QR — é o papel de quem imprime —
 * mas não muda nada.
 */

import type { PanelUser } from "@/lib/auth/panel";
import { env } from "@/lib/env";
import { ApiError } from "@/lib/http";

export function requireMenuEditor(user: PanelUser): void {
  if (user.role !== "owner" && user.role !== "manager") {
    throw new ApiError(403, "Somente dono ou gerente altera o cardápio.");
  }
}

/** A URL completa que vai no QR. */
export function menuUrl(request: Request, token: string): string {
  return `${publicOrigin(request)}/cardapio/${encodeURIComponent(token)}`;
}

export function publicOrigin(request: Request): string {
  if (env.publicBaseUrl) return env.publicBaseUrl;
  // Atrás do proxy do Coolify o host real vem no `x-forwarded-*`. O pedido é
  // de quem está logado no painel: forjar o cabeçalho só estragaria o QR da
  // própria pessoa, e `PUBLIC_BASE_URL` fecha até isso.
  const forwardedHost = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim();
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const url = new URL(request.url);
  const host = forwardedHost || request.headers.get("host") || url.host;
  const proto = forwardedProto || url.protocol.replace(":", "");
  return `${proto}://${host}`;
}

/** Rótulo de mesa: opcional, curto, sem quebra de linha. */
export function normalizeTableLabel(raw: unknown): string | null {
  const text = String(raw ?? "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  if (text.length > 40) throw new ApiError(422, "O nome da mesa tem no máximo 40 caracteres.");
  return text;
}
