/**
 * Sessão de quem abre o painel administrativo.
 *
 * Duas identidades, dois mecanismos
 * ---------------------------------
 *
 * O **terminal** se autentica por token de longa duração (ver `device.ts`):
 * ele é uma máquina, roda sozinho e precisa sincronizar às três da manhã. A
 * **pessoa** que abre o painel se autentica por senha e recebe uma sessão
 * curta, em cookie `HttpOnly`. Misturar os dois daria ao painel um token que
 * não expira, ou ao terminal uma sessão que exige alguém digitar senha.
 *
 * O hash da senha vive em `password.ts`, sem o pool do banco junto: o
 * script que cria o primeiro usuário precisa gerar um hash antes de existir
 * qualquer sessão. Ver o cabeçalho de lá para a escolha do algoritmo.
 */

import { createHash, randomBytes } from "node:crypto";

import { hashPassword, verifyPassword } from "@/lib/auth/password";
import { sql } from "@/lib/db";
import { ApiError } from "@/lib/http";

/** Duração da sessão do painel. Curta: aqui se emite comando para a loja. */
const SESSION_HOURS = 12;

// Reexportados para quem ja importava daqui; a implementacao mudou de
// arquivo, o contrato nao.
export { hashPassword, verifyPassword };

export const SESSION_COOKIE = "erp_session";

export interface PanelUser {
  id: string;
  tenantId: string;
  name: string;
  email: string;
  role: string;
  canAuthorize: boolean;
  maxDiscountPercent: number;
}

// --------------------------------------------------------------------------- //
// Sessão
// --------------------------------------------------------------------------- //

export function hashSessionToken(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

export async function openSession(
  user: PanelUser,
  meta: { ip: string; userAgent: string },
): Promise<{ token: string; expiresAt: Date }> {
  const token = randomBytes(32).toString("base64url");
  const expiresAt = new Date(Date.now() + SESSION_HOURS * 3600 * 1000);

  await sql`
    INSERT INTO panel_sessions
      (token_hash, tenant_id, user_id, expires_at, ip, user_agent)
    VALUES (${hashSessionToken(token)}, ${user.tenantId}, ${user.id},
            ${expiresAt}, ${meta.ip}, ${meta.userAgent.slice(0, 256)})
  `;

  return { token, expiresAt };
}

export async function closeSession(token: string | null | undefined): Promise<boolean> {
  if (!token) return false;
  const result = await sql`
    UPDATE panel_sessions SET revoked_at = now()
     WHERE token_hash = ${hashSessionToken(token)} AND revoked_at IS NULL
  `;
  return result.count > 0;
}

/**
 * Resolve o cookie de sessão numa pessoa.
 *
 * @throws ApiError 401 — sem sessão, vencida ou revogada.
 */
export async function requirePanelUser(request: Request): Promise<PanelUser> {
  const token = readCookie(request, SESSION_COOKIE);
  if (!token) throw new ApiError(401, "Entre no painel para continuar.");

  const rows = await sql<
    {
      id: string;
      tenant_id: string;
      name: string;
      email: string;
      role: string;
      can_authorize: boolean;
      max_discount_percent: string;
    }[]
  >`
    SELECT u.id, u.tenant_id, u.name, u.email, u.role,
           u.can_authorize, u.max_discount_percent
      FROM panel_sessions s
      JOIN panel_users u ON u.id = s.user_id
     WHERE s.token_hash = ${hashSessionToken(token)}
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND u.is_active
  `;

  const row = rows[0];
  if (!row) throw new ApiError(401, "Sessão expirada. Entre de novo.");

  return {
    id: row.id,
    tenantId: row.tenant_id,
    name: row.name,
    email: row.email,
    role: row.role,
    canAuthorize: row.can_authorize,
    maxDiscountPercent: Number(row.max_discount_percent),
  };
}

export function sessionCookie(token: string, expiresAt: Date): string {
  const attributes = [
    `${SESSION_COOKIE}=${token}`,
    "Path=/",
    // HttpOnly: um XSS no painel não consegue ler o cookie e roubar a sessão
    // de quem pode emitir desconto em todas as lojas.
    "HttpOnly",
    // SameSite=Lax barra o CSRF vindo de outro site sem quebrar o login normal.
    "SameSite=Lax",
    `Expires=${expiresAt.toUTCString()}`,
  ];
  // `Secure` só em produção: em desenvolvimento o painel roda em http://localhost
  // e o navegador descartaria o cookie, transformando "login não funciona" num
  // mistério de meia hora.
  if (process.env.NODE_ENV === "production") attributes.push("Secure");
  return attributes.join("; ");
}

export function clearedCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`;
}

function readCookie(request: Request, name: string): string | null {
  const header = request.headers.get("cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=") || null;
  }
  return null;
}
