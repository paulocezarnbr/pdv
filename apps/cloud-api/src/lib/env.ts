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

const REQUIRED = ["DATABASE_URL", "SESSION_SECRET"] as const;

/**
 * O serviço fiscal fica FORA da lista obrigatória, e a diferença importa.
 *
 * Numa versão anterior as duas variáveis fiscais eram obrigatórias, e o
 * `/api/health` passava a responder 503 sem elas. O efeito era que um deploy
 * sem o serviço fiscal — que ainda está sob trava de homologação, ver
 * `docs/fiscal_architecture.md` — nunca ficava verde no Coolify, e a
 * sincronização (o caminho antifraude, que não depende de NFC-e) ficava
 * refém de um recurso que ainda não pode ser usado em produção.
 *
 * Agora a ausência do fiscal é um estado visível (`fiscal: "desligado"` no
 * health, 503 com mensagem clara em `/api/fiscal/*`) e não uma falha do
 * processo inteiro.
 */
const FISCAL = ["FISCAL_SERVICE_URL", "FISCAL_SERVICE_TOKEN"] as const;

type RequiredName = (typeof REQUIRED)[number] | (typeof FISCAL)[number];

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

  /** As duas variáveis do serviço fiscal estão presentes. */
  get fiscalConfigured(): boolean {
    return FISCAL.every((name) => Boolean(process.env[name]?.trim()));
  },

  /**
   * Liberação explícita para emitir no ambiente de **produção** da SEFAZ.
   *
   * Existe porque a trava de homologação vive no serviço fiscal, e a nuvem
   * reserva o número **antes** de chamá-lo. Sem esta variável, uma loja
   * configurada como `production` queimaria um número real da série a cada
   * venda, o motor travado o rejeitaria, e cada buraco na numeração exigiria
   * inutilização formal na SEFAZ. A trava daqui age antes da reserva.
   *
   * Só deve ser ligada depois que o motor passar na homologação RJ/SVRS.
   */
  /**
   * Endereço público da retaguarda, para montar o link impresso no QR do
   * cardápio. Opcional: sem ele o painel usa o endereço pelo qual foi aberto,
   * que no Coolify é o domínio configurado. Definir evita que um QR gerado a
   * partir de um acesso por IP ou por domínio provisório vá impresso na mesa.
   */
  get publicBaseUrl(): string | null {
    const value = process.env.PUBLIC_BASE_URL?.trim();
    return value ? value.replace(/\/+$/, "") : null;
  },

  get fiscalProductionEnabled(): boolean {
    return process.env.FISCAL_PRODUCTION_ENABLED === "true";
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
