"use client";

/**
 * O widget do Cloudflare Turnstile, carregado só quando o servidor tem a chave.
 *
 * Renderização explícita: o script padrão procura `.cf-turnstile` no carregamento
 * da página, e o React monta o formulário depois disso. Cada token vale uma vez
 * — depois de um login recusado, o pai troca `resetKey` e o widget gera outro.
 */

import { useEffect, useRef } from "react";

type Turnstile = {
  render(element: HTMLElement, options: Record<string, unknown>): string;
  reset(widgetId?: string): void;
  remove(widgetId: string): void;
};

declare global {
  interface Window {
    turnstile?: Turnstile;
  }
}

const SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

let loading: Promise<Turnstile> | null = null;

function loadTurnstile(): Promise<Turnstile> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  loading ??= new Promise<Turnstile>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCRIPT_URL;
    script.async = true;
    script.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error("Turnstile indisponível")));
    script.onerror = () => {
      loading = null;
      reject(new Error("Não foi possível carregar a verificação de segurança. Confira a conexão e recarregue a página."));
    };
    document.head.appendChild(script);
  });
  return loading;
}

export function TurnstileWidget({
  siteKey,
  resetKey,
  onToken,
  onError,
}: {
  siteKey: string;
  resetKey: number;
  onToken: (token: string) => void;
  onError: (message: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const widget = useRef<string | null>(null);
  // Os callbacks mudam a cada render do pai; o widget é criado uma vez.
  const handlers = useRef({ onToken, onError });
  handlers.current = { onToken, onError };

  useEffect(() => {
    let cancelled = false;
    loadTurnstile()
      .then((turnstile) => {
        if (cancelled || !container.current || widget.current) return;
        widget.current = turnstile.render(container.current, {
          sitekey: siteKey,
          action: "login",
          theme: "dark",
          language: "pt-br",
          callback: (token: string) => handlers.current.onToken(token),
          "expired-callback": () => handlers.current.onToken(""),
          "error-callback": () => {
            handlers.current.onToken("");
            handlers.current.onError("A verificação de segurança não carregou. Recarregue a página.");
          },
        });
      })
      .catch((reason: unknown) => handlers.current.onError(reason instanceof Error ? reason.message : String(reason)));
    return () => {
      cancelled = true;
      if (widget.current && window.turnstile) window.turnstile.remove(widget.current);
      widget.current = null;
    };
  }, [siteKey]);

  useEffect(() => {
    if (resetKey > 0 && widget.current && window.turnstile) window.turnstile.reset(widget.current);
  }, [resetKey]);

  return <div ref={container} className="turnstile-box" aria-label="Verificação de segurança" />;
}
