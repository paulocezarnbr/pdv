/**
 * Respostas e erros da API.
 *
 * O padrão de erro é o mesmo do FastAPI que existia antes (`{"detail": "..."}`)
 * porque o terminal já lê esse formato: `sync/transport.py` e
 * `remote/commands.py` procuram `detail`. Mudar o envelope obrigaria a
 * atualizar todos os PDVs instalados antes de publicar a nuvem nova — que é o
 * tipo de acoplamento que trava o deploy.
 */

import { NextResponse } from "next/server";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly headers?: Record<string, string>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function json<T>(body: T, init?: ResponseInit): NextResponse {
  return NextResponse.json(body, init);
}

export function fail(status: number, detail: string, headers?: Record<string, string>) {
  return NextResponse.json({ detail }, { status, headers });
}

/**
 * Embrulha um handler e converte exceção em resposta.
 *
 * O `catch` genérico devolve 500 com uma mensagem fixa e joga o erro real no
 * log do servidor. Vazar `error.message` para o cliente entregaria nome de
 * tabela, texto de SQL e caminho de arquivo a quem estiver sondando a API — e
 * o terminal não tem o que fazer com essa informação de qualquer forma.
 */
export function handler(
  fn: (request: Request) => Promise<NextResponse>,
): (request: Request) => Promise<NextResponse> {
  return async (request: Request) => {
    try {
      return await fn(request);
    } catch (error) {
      if (error instanceof ApiError) {
        return fail(error.status, error.message, error.headers);
      }
      console.error("[api] erro não tratado", {
        url: request.url,
        method: request.method,
        error,
      });
      return fail(500, "Erro interno. A requisição não foi aplicada.");
    }
  };
}

/** Corpo JSON validado por um schema do zod, com 422 em vez de 500. */
export async function parseBody<T>(
  request: Request,
  schema: { safeParse: (data: unknown) => { success: boolean; data?: T; error?: unknown } },
): Promise<T> {
  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    throw new ApiError(400, "Corpo da requisição não é JSON válido.");
  }

  const parsed = schema.safeParse(raw);
  if (!parsed.success || parsed.data === undefined) {
    const issues = (parsed.error as { issues?: { path: (string | number)[]; message: string }[] })
      ?.issues;
    const detail =
      issues
        ?.map((issue) => `${issue.path.join(".") || "corpo"}: ${issue.message}`)
        .join("; ") ?? "Corpo inválido.";
    throw new ApiError(422, detail);
  }
  return parsed.data;
}

/**
 * O IP de origem, para os limitadores de tentativa e a auditoria. A regra —
 * Cloudflare, `X-Forwarded-For` pela direita, e a premissa de proxy — está em
 * `client-ip.ts`.
 */
export { clientIp } from "./client-ip";
