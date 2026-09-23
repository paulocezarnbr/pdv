/**
 * Healthcheck — é o que o Coolify consulta antes de trocar o tráfego.
 *
 * Responde 503 quando o Postgres não responde, e isso é deliberado: no Coolify
 * o deploy só promove o contêiner novo depois que o healthcheck fica verde. Um
 * `/health` que devolve 200 sem olhar o banco faria o deploy promover uma API
 * que sobe, atende, e responde 500 em toda rota — derrubando a versão anterior,
 * que funcionava, para colocar no ar uma que não funciona.
 *
 * O que ele **não** faz: contar linha, varrer tabela, checar migration. Um
 * healthcheck que consulta dados vira carga fixa a cada trinta segundos, para
 * sempre, e passa a falhar por lentidão em vez de por indisponibilidade.
 */

import { pingDatabase, rlsStatus } from "@/lib/db";
import { assertEnv, env } from "@/lib/env";
import { json } from "@/lib/http";

// Nada aqui pode ser cacheado nem pré-renderizado: a resposta é sobre o estado
// deste processo agora.
export const dynamic = "force-dynamic";
export const revalidate = 0;

const STARTED_AT = Date.now();

export async function GET(): Promise<Response> {
  // Antes de tocar no banco: variável faltando não é problema de banco, e
  // tentar conectar primeiro mostraria "banco indisponível" para quem só
  // esqueceu de cadastrar a URL.
  const missing = assertEnv();
  if (missing.length > 0) {
    console.error("[health] variáveis obrigatórias ausentes:", missing);
    return json(
      {
        service: "erpfood-cloud-api",
        version: process.env.APP_VERSION ?? "dev",
        database: "não configurado",
        missing_env: missing,
      },
      { status: 503 },
    );
  }

  const database = await pingDatabase();

  const body = {
    service: "erpfood-cloud-api",
    version: process.env.APP_VERSION ?? "dev",
    uptime_seconds: Math.round((Date.now() - STARTED_AT) / 1000),
    database: database.ok ? "ok" : "indisponível",
    // `inerte` significa que a aplicação conecta como superusuário e a
    // política de isolamento por tenant no banco não vale (ver `lib/db.ts`).
    // Não derruba o healthcheck — o filtro da aplicação continua valendo —,
    // mas fica à vista em vez de escondido.
    tenant_isolation: database.ok ? await rlsStatus() : "desconhecido",
    // Estado, não requisito. A emissão fiscal ainda está sob trava de
    // homologação, e a sincronização não depende dela: um deploy sem o
    // serviço fiscal precisa ficar verde. Ver `lib/env.ts`.
    fiscal: !env.fiscalConfigured
      ? "desligado"
      : env.fiscalProductionEnabled
        ? "produção liberada"
        : "somente homologação",
  };

  if (!database.ok) {
    // O motivo vai para o log do contêiner, não para o corpo: a string de erro
    // do driver carrega host, porta e nome de banco.
    console.error("[health] banco indisponível:", database.error);
    return json(body, { status: 503 });
  }

  return json(body);
}

/**
 * O `HEAD` existe porque é o que a maioria dos monitores usa por padrão, e um
 * 405 no monitor apareceria como "serviço fora" sem ele estar.
 */
export async function HEAD(): Promise<Response> {
  if (assertEnv().length > 0) return new Response(null, { status: 503 });
  const database = await pingDatabase();
  return new Response(null, { status: database.ok ? 200 : 503 });
}
