/**
 * Configuração do processo, validada **no primeiro uso**.
 *
 * Por que falhar, e falhar claro
 * ------------------------------
 *
 * Um `process.env.X!` espalhado pelo código transforma variável ausente em
 * `undefined` viajando silenciosamente até virar um `WHERE tenant_id = NULL`
 * que não casa com nada — ou, pior, uma chave de assinatura vazia. O erro
 * aparece como "o terminal parou de sincronizar", horas depois do deploy, sem
 * nada no log ligando uma coisa à outra.
 *
 * Aqui a leitura é centralizada e a ausência vira uma exceção que **diz o nome
 * da variável**. No Coolify isso é o que se quer: o healthcheck nunca fica
 * verde, o deploy anterior continua no ar, e o log da tentativa aponta o que
 * falta.
 *
 * Por que no primeiro uso, e não na carga do módulo
 * -------------------------------------------------
 *
 * A primeira versão validava dentro de um `export const env = {...}`, avaliado
 * ao importar. Isso quebrou o `docker build`: o `next build` importa cada rota
 * para coletar os metadados dela, e a importação disparava a validação. O
 * build passou a exigir `DATABASE_URL` — uma credencial de produção que teria
 * de ser passada como argumento de build, onde ficaria gravada na imagem.
 *
 * O detalhe é que isso não aparecia localmente: rodando `next build` com
 * valores de teste no ambiente, tudo passava. Quem mostrou foi o build da
 * imagem, que é o único lugar onde o ambiente está realmente vazio.
 *
 * Com `get`, a validação acontece quando alguém precisa do valor de verdade —
 * na primeira requisição — e o build não precisa de segredo nenhum.
 */

const REQUIRED = [
  "DATABASE_URL",
  "SESSION_SECRET",
  "FISCAL_SERVICE_URL",
  "FISCAL_SERVICE_TOKEN",
] as const;

type RequiredName = (typeof REQUIRED)[number];

function required(name: RequiredName): string {
  const value = process.env[name];
  if (!value || !value.trim()) {
    throw new Error(
      `Variável de ambiente obrigatória ausente: ${name}. ` +
        `Ver .env.example e COOLIFY.md.`,
    );
  }
  return value.trim();
}

function optionalInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (Number.isNaN(parsed) || parsed <= 0) {
    throw new Error(
      `${name} precisa ser um inteiro positivo, veio ${JSON.stringify(raw)}`,
    );
  }
  return parsed;
}

export const env = {
  get databaseUrl(): string {
    return required("DATABASE_URL");
  },

  /**
   * Chave de sessão do painel. NÃO é a chave HMAC do ledger de auditoria —
   * essa é por terminal e nunca sai do terminal (ver `provisioning/secrets.py`
   * no desktop). Trocar esta aqui derruba as sessões do painel e nada mais.
   */
  get sessionSecret(): string {
    return required("SESSION_SECRET");
  },

  /**
   * Quantas conexões este processo abre. O padrão é baixo de propósito: o
   * Coolify costuma rodar o Postgres no mesmo host, com `max_connections`
   * padrão de 100, e três réplicas com pool de 20 esgotariam o banco antes de
   * esgotar a CPU.
   */
  get poolMax(): number {
    return optionalInt("DATABASE_POOL_MAX", 10);
  },

  /** Porta. O Coolify injeta `PORT`; localmente cai em 3000. */
  get port(): number {
    return optionalInt("PORT", 3000);
  },

  get nodeEnv(): string {
    return process.env.NODE_ENV ?? "development";
  },

  get isProduction(): boolean {
    return this.nodeEnv === "production";
  },

  /** Serviço fiscal interno. Nunca deve ser publicado pelo proxy. */
  get fiscalServiceUrl(): string {
    return required("FISCAL_SERVICE_URL");
  },

  get fiscalServiceToken(): string {
    return required("FISCAL_SERVICE_TOKEN");
  },

  get fiscalTimeoutMs(): number {
    return optionalInt("FISCAL_SERVICE_TIMEOUT_MS", 20_000);
  },
} as const;

/**
 * Confere tudo de uma vez, antes de atender.
 *
 * Chamado pelo `/api/health`: é o que faz a ausência de uma variável virar um
 * healthcheck vermelho no Coolify, em vez de um 500 na primeira venda.
 */
export function assertEnv(): string[] {
  const missing: string[] = [];
  for (const name of REQUIRED) {
    try {
      required(name);
    } catch {
      missing.push(name);
    }
  }
  return missing;
}
