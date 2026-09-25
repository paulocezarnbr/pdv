/**
 * `GET /api/panel/login-config` — o que a tela de login precisa antes de
 * alguém entrar: hoje, a chave pública do Cloudflare Turnstile.
 *
 * Servida em tempo de execução, e não como `NEXT_PUBLIC_*`: variável pública
 * do Next é fixada no build, e trocar a chave no Coolify exigiria reconstruir
 * a imagem. A chave do site é pública por natureza; a secreta nunca sai daqui.
 */

import { turnstileConfig } from "@/lib/auth/turnstile";
import { handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

export const GET = handler(async () => {
  const { siteKey, required } = turnstileConfig();
  return json(
    { turnstile_site_key: siteKey, turnstile_required: required },
    { headers: { "Cache-Control": "no-store" } },
  );
});
