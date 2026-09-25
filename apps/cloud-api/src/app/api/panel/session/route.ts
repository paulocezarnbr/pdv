/**
 * Login e logout do painel.
 *
 * O freio de tentativas
 * ---------------------
 *
 * Fica por **e-mail e por IP**, somando os dois. Só por IP, um atacante atrás
 * de várias saídas passa livre; só por e-mail, ele trava a conta do dono de
 * propósito para tirá-lo do ar — que é um ataque de negação de serviço barato
 * demais para deixar aberto. Exigir os dois tetos cobre os dois casos.
 *
 * A mensagem de erro é sempre a mesma
 * -----------------------------------
 *
 * "E-mail ou senha inválidos", para e-mail inexistente, senha errada e conta
 * desativada. Distinguir os casos entregaria quais contas existem antes de o
 * atacante tentar a primeira senha. E o custo do scrypt é pago mesmo quando o
 * e-mail não existe, senão a resposta imediata diria a mesma coisa pelo tempo.
 */

import { z } from "zod";

import {
  clearedCookie,
  closeSession,
  hashPassword,
  openSession,
  sessionCookie,
  SESSION_COOKIE,
  verifyPassword,
  type PanelUser,
  requirePanelUser,
} from "@/lib/auth/panel";
import { verifyTurnstile } from "@/lib/auth/turnstile";
import { sql } from "@/lib/db";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  return json({
    user: {
      name: user.name,
      email: user.email,
      role: user.role,
      canAuthorize: user.canAuthorize,
    },
  });
});

const MAX_ATTEMPTS = 8;
const WINDOW_MINUTES = 15;

const LoginSchema = z.object({
  email: z.string().min(3).max(255),
  password: z.string().min(1).max(256),
  /** Token do Cloudflare Turnstile; exigido quando `TURNSTILE_SECRET_KEY` está definido. */
  turnstile_token: z.string().max(4096).optional(),
});

//: Hash descartável, com os mesmos parâmetros dos reais, para gastar o mesmo
//: tempo quando o e-mail não existe. Calculado na primeira necessidade e
//: guardado: gerá-lo a cada tentativa custaria o dobro do tempo de um login
//: legítimo, e transformaria o próprio anti-enumeração num vetor de DoS.
let dummyHash: Promise<string> | null = null;
function burnHash(): Promise<string> {
  dummyHash ??= hashPassword("senha-que-nao-existe-" + Math.random());
  return dummyHash;
}

export const POST = handler(async (request) => {
  const body = await parseBody(request, LoginSchema);
  const ip = clientIp(request);
  const email = body.email.trim().toLowerCase();

  // Antes do freio e da senha: robô barrado aqui não gasta tentativa da conta
  // nem o scrypt do servidor.
  await verifyTurnstile(body.turnstile_token, ip);
  await assertNotThrottled(email, ip);

  const rows = await sql<
    {
      id: string;
      tenant_id: string;
      name: string;
      email: string;
      role: string;
      password_hash: string;
      can_authorize: boolean;
      max_discount_percent: string;
      is_active: boolean;
    }[]
  >`
    SELECT id, tenant_id, name, email, role, password_hash,
           can_authorize, max_discount_percent, is_active
      FROM panel_users
     WHERE lower(email) = ${email}
     LIMIT 1
  `;

  const row = rows[0];
  const stored = row?.is_active ? row.password_hash : await burnHash();
  const ok = await verifyPassword(stored, body.password);

  if (!row || !row.is_active || !ok) {
    await registerFailure(email, ip);
    throw new ApiError(401, "E-mail ou senha inválidos.");
  }

  await clearFailures(email);

  const user: PanelUser = {
    id: row.id,
    tenantId: row.tenant_id,
    name: row.name,
    email: row.email,
    role: row.role,
    canAuthorize: row.can_authorize,
    maxDiscountPercent: Number(row.max_discount_percent),
  };

  const session = await openSession(user, {
    ip,
    userAgent: request.headers.get("user-agent") ?? "",
  });

  return json(
    { user: { name: user.name, email: user.email, role: user.role } },
    { headers: { "Set-Cookie": sessionCookie(session.token, session.expiresAt) } },
  );
});

export const DELETE = handler(async (request) => {
  const cookie = request.headers.get("cookie") ?? "";
  const token = cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${SESSION_COOKIE}=`))
    ?.slice(SESSION_COOKIE.length + 1);

  const ended = await closeSession(token);
  return json({ ended }, { headers: { "Set-Cookie": clearedCookie() } });
});

// --------------------------------------------------------------------------- //
// Freio
// --------------------------------------------------------------------------- //

async function assertNotThrottled(email: string, ip: string): Promise<void> {
  const rows = await sql<{ scope: string; failures: number }[]>`
    SELECT scope, failures FROM panel_login_throttle
     WHERE scope = ANY(${[`email:${email}`, `ip:${ip}`]})
       AND locked_until > now()
  `;
  if (rows.length > 0) {
    throw new ApiError(429, "Muitas tentativas. Aguarde alguns minutos.");
  }
}

async function registerFailure(email: string, ip: string): Promise<void> {
  for (const scope of [`email:${email}`, `ip:${ip}`]) {
    await sql`
      INSERT INTO panel_login_throttle
        (scope, failures, first_failure_at, last_failure_at, locked_until)
      VALUES (${scope}, 1, now(), now(), NULL)
      ON CONFLICT (scope) DO UPDATE SET
        -- Falha antiga deixa de contar: sem a janela, oito erros espalhados
        -- por seis meses travariam quem nunca foi atacado.
        failures = CASE
          WHEN panel_login_throttle.first_failure_at
               > now() - ${`${WINDOW_MINUTES} minutes`}::interval
          THEN panel_login_throttle.failures + 1
          ELSE 1 END,
        first_failure_at = CASE
          WHEN panel_login_throttle.first_failure_at
               > now() - ${`${WINDOW_MINUTES} minutes`}::interval
          THEN panel_login_throttle.first_failure_at
          ELSE now() END,
        last_failure_at = now(),
        locked_until = CASE
          WHEN panel_login_throttle.failures + 1 >= ${MAX_ATTEMPTS}
          THEN now() + ${`${WINDOW_MINUTES} minutes`}::interval
          ELSE NULL END
    `;
  }
}

async function clearFailures(email: string): Promise<void> {
  // Limpa o escopo do e-mail e **não** o do IP: limpar o do IP no primeiro
  // acerto daria ao atacante uma saída barata — errar sete vezes, acertar o
  // próprio login e recomeçar do zero. O do IP decai sozinho pela janela.
  await sql`DELETE FROM panel_login_throttle WHERE scope = ${`email:${email}`}`;
}
