import type { NextConfig } from "next";

const config: NextConfig = {
  // `standalone` e o que torna a imagem pequena o bastante para um deploy
  // rapido no Coolify: o Next copia para `.next/standalone` apenas o runtime e
  // as dependencias efetivamente alcancadas. Sem isto a imagem carregaria o
  // `node_modules` inteiro — centenas de MB que nunca sao executados.
  output: "standalone",

  // Nada aqui e estatico: toda rota fala com o Postgres. Sem esta linha o
  // `next build` tentaria pre-renderizar as rotas, falharia por nao ter banco
  // no ambiente de build, e o erro apareceria como "build failed" no Coolify
  // sem dizer que o problema e a ausencia de DATABASE_URL — que nem deveria
  // estar la.
  experimental: {
    serverActions: { bodySizeLimit: "2mb" },
  },

  // O terminal e o painel sao os unicos clientes. Nao ha imagem remota, nao ha
  // telemetria: superficie que nao serve a ninguem e so risco.
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          // O terminal fala HTTPS com a nuvem; o HSTS impede que uma primeira
          // requisicao em claro seja possivel depois da primeira visita.
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains",
          },
        ],
      },
    ];
  },
};

export default config;
