/**
 * Cloudflare Turnstile — a prova de "não é robô" no login do painel.
 *
 * O freio de tentativas por e-mail e IP já limita a força bruta; o Turnstile
 * tira do caminho o robô antes de ele gastar uma tentativa — e antes de o
 * servidor gastar um scrypt com ele.
 *
 * Ligado por ambiente
 * -------------------
 *
 * `TURNSTILE_SECRET_KEY` liga a exigência; `TURNSTILE_SITE_KEY` é a chave
 * pública que o navegador usa para desenhar o widget. As duas vêm do painel
 * da Cloudflare (Turnstile → Add widget, com o domínio do painel). Sem a
 * secreta, o login funciona como antes — é o caso dos testes e do
 * desenvolvimento local.
 *
 * Falha fechada
 * -------------
 *
 * Se a Cloudflare não responder, o login é recusado com 503. Aceitar "porque
 * não deu para conferir" transformaria qualquer falha de rede provocada num
 * desvio do captcha.
 */

import { ApiError } from "@/lib/http";

export const TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";

/** A ação declarada no widget do login; a resposta da Cloudflare a devolve. */
export const LOGIN_ACTION = "login";

export interface TurnstileConfig {
  /** Chave pública para o widget, ou `null` quando o captcha está desligado. */
  siteKey: string | null;
  /** O servidor exige o captcha. */
  required: boolean;
}

export function turnstileConfig(): TurnstileConfig {
  const siteKey = process.env.TURNSTILE_SITE_KEY?.trim() || null;
  const required = Boolean(process.env.TURNSTILE_SECRET_KEY?.trim());
  return { siteKey, required };
}

type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * Confere o token do widget com a Cloudflare.
 *
 * @throws ApiError 400 — o captcha está ligado e o token não veio.
 * @throws ApiError 403 — a Cloudflare recusou o token (vencido, reusado, de outro site).
 * @throws ApiError 503 — a Cloudflare não respondeu.
 */
export async function verifyTurnstile(
  token: string | undefined | null,
  ip: string,
  fetchImpl: Fetch = fetch,
): Promise<void> {
  const secret = process.env.TURNSTILE_SECRET_KEY?.trim();
  if (!secret) return;

  if (!token?.trim()) {
    throw new ApiError(400, "Confirme a verificação de segurança antes de entrar.");
  }

  const form = new URLSearchParams({ secret, response: token.trim() });
  if (ip && ip !== "desconhecido") form.set("remoteip", ip);

  let outcome: { success?: boolean; action?: string; "error-codes"?: string[] };
  try {
    const response = await fetchImpl(TURNSTILE_VERIFY_URL, {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(8000),
    });
    outcome = (await response.json()) as typeof outcome;
  } catch (error) {
    console.error("[turnstile] verificação indisponível", error);
    throw new ApiError(
      503,
      "Não foi possível confirmar a verificação de segurança agora. Tente de novo em instantes.",
    );
  }

  // A ação amarra o token ao login: um token resolvido em outra tela deste
  // site não serve aqui.
  if (outcome.success !== true || (outcome.action && outcome.action !== LOGIN_ACTION)) {
    console.warn("[turnstile] token recusado", { ip, codes: outcome["error-codes"] ?? [] });
    throw new ApiError(403, "A verificação de segurança falhou. Recarregue a página e tente de novo.");
  }
}
