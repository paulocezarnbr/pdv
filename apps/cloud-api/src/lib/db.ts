/**
 * A conexão com o Postgres.
 *
 * Sem ORM, de propósito
 * ---------------------
 *
 * O mesmo motivo do lado do terminal (ver `data/database.py`): o controle
 * transacional aqui precisa ser explícito e visível. Um lote de sincronização
 * é **uma** transação — venda, itens, estoque e auditoria entram juntos ou não
 * entram —, e é exatamente esse tipo de garantia que um ORM esconde atrás de
 * uma sessão implícita que faz commit quando acha que deve.
 *
 * Uma instância por processo
 * --------------------------
 *
 * Em desenvolvimento o Next recarrega os módulos a cada alteração, e cada
 * recarga criaria um pool novo. Vinte edições depois o Postgres recusa conexão
 * — e o sintoma é "o banco caiu", não "o hot reload vazou pool". Guardar no
 * `globalThis` é o padrão que resolve isso; em produção o módulo carrega uma
 * vez só e a guarda não custa nada.
 */

import postgres from "postgres";

import { env } from "@/lib/env";

declare global {
  // eslint-disable-next-line no-var
  var __erpSql: postgres.Sql | undefined;
}

function create(): postgres.Sql {
  return postgres(env.databaseUrl, {
    max: env.poolMax,

    // Conexão ociosa devolvida ao sistema operacional depois de 30 s. Num
    // deploy do Coolify com pouca carga noturna, segurar dez conexões abertas
    // a noite inteira só gasta memória do Postgres.
    idle_timeout: 30,

    // Teto para a abertura de conexão. Sem ele, um Postgres fora do ar segura
    // a requisição até o timeout do proxy, e a API vai degradando até parar —
    // com o banco visivelmente caído, o que torna o diagnóstico pior.
    connect_timeout: 10,

    // O `onnotice` padrão despeja NOTICE no stdout. Em produção isso polui o
    // log do Coolify com ruído do Postgres que ninguém lê.
    onnotice: env.isProduction ? () => {} : undefined,

    transform: { undefined: null },
  });
}

let client: postgres.Sql | undefined;

/**
 * O pool, criado na **primeira consulta** e não na carga do módulo.
 *
 * A diferença aparece no `next build`. O build importa cada rota para coletar
 * os metadados dela, e importar a rota importa este módulo. Criando o cliente
 * aqui em cima, o build passaria a exigir uma `DATABASE_URL` sintaticamente
 * válida só para o `postgres()` conseguir fazer o parse — e falharia com
 * `TypeError: Invalid URL`, sem dizer que o problema é uma variável que nem
 * deveria existir naquele momento.
 *
 * Isso importa no Coolify: a credencial de produção teria de ser passada como
 * argumento de build, onde ficaria gravada na imagem. Adiar a criação mantém
 * o build sem segredo nenhum.
 */
function instance(): postgres.Sql {
  if (globalThis.__erpSql) return globalThis.__erpSql;
  client ??= create();
  // Em desenvolvimento o Next recarrega os módulos a cada alteração, e cada
  // recarga criaria um pool novo. Vinte edições depois o Postgres recusa
  // conexão — e o sintoma é "o banco caiu", não "o hot reload vazou pool".
  if (!env.isProduction) globalThis.__erpSql = client;
  return client;
}

/**
 * A mesma interface de sempre (`sql\`SELECT …\``, `sql.begin`, `sql.json`),
 * resolvida na hora do uso. O `Proxy` existe para que as dezenas de chamadas
 * espalhadas pelas rotas não precisem saber que a criação é tardia.
 */
export const sql: postgres.Sql = new Proxy((() => {}) as unknown as postgres.Sql, {
  apply(_target, _thisArg, args: unknown[]) {
    return (instance() as unknown as (...a: unknown[]) => unknown)(...args);
  },
  get(_target, property, receiver) {
    return Reflect.get(instance() as object, property, receiver);
  },
  has(_target, property) {
    return Reflect.has(instance() as object, property);
  },
});

/**
 * Confere que o banco responde. É o que o `/api/health` consulta.
 *
 * `SELECT 1` e não uma consulta de negócio: o healthcheck responde "dá para
 * atender", não "os dados estão certos". Um healthcheck que varre tabela vira
 * carga fixa a cada trinta segundos, para sempre.
 */
export async function pingDatabase(): Promise<{ ok: boolean; error?: string }> {
  try {
    await sql`SELECT 1`;
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * O isolamento por tenant no banco está realmente valendo?
 *
 * Existe porque a resposta pode ser **não** sem que nada quebre. O Postgres
 * gerenciado do Coolify cria o usuário como superusuário, e superusuário tem
 * `rolbypassrls`: ignora toda política, com ou sem `FORCE`. Nesse arranjo a
 * política de isolamento fica no schema, passa na revisão, e não barra nada.
 *
 * Um teste de integração pegou isso. Esta função é o que impede que volte
 * calado: o `/api/health` publica o estado, então a diferença entre "tem
 * segunda barreira" e "não tem" fica visível sem ninguém precisar inspecionar
 * `pg_roles` para descobrir.
 *
 * Note que **inerte não é fora do ar**: o filtro por tenant da aplicação
 * continua valendo em toda consulta. O que se perde é a rede de segurança para
 * o dia em que alguém escrever uma consulta nova e esquecer o `WHERE`.
 */
export async function rlsStatus(): Promise<"ativo" | "inerte" | "desconhecido"> {
  try {
    const rows = await sql<{ bypasses: boolean }[]>`
      SELECT (rolsuper OR rolbypassrls) AS bypasses
        FROM pg_roles WHERE rolname = current_user
    `;
    const row = rows[0];
    if (row === undefined) return "desconhecido";
    return row.bypasses ? "inerte" : "ativo";
  } catch {
    return "desconhecido";
  }
}

export type Tx = postgres.TransactionSql;

/**
 * Abre uma transação **já declarando de qual tenant ela é**.
 *
 * Toda rota que escreve passa por aqui. A alternativa — confiar na saída da
 * política, que permite tudo quando `app.tenant_id` não está definido — se
 * mostrou frágil de um jeito difícil de achar: um GUC customizado no Postgres
 * não volta a ser NULL depois de definido uma vez naquela conexão, ele volta a
 * ser string vazia. A conexão do pool que já tinha atendido um `/sync/push`
 * passava a bloquear tudo nas rotas que não declaravam o tenant, e o sintoma
 * era uma falha intermitente que dependia de qual conexão o pool entregasse.
 *
 * Ver `migrations/005_rls_escape_hatch.sql`. A política foi corrigida; isto
 * aqui é a outra metade: o caminho quente não depende mais da saída dela.
 */
export async function withTenant<T>(
  tenantId: string,
  work: (tx: Tx) => Promise<T>,
): Promise<T> {
  return sql.begin(async (tx) => {
    // `SET LOCAL`, via `set_config(..., true)`: morre com a transação, então a
    // conexão devolvida ao pool não carrega o tenant da requisição anterior.
    await tx`SELECT set_config('app.tenant_id', ${tenantId}, true)`;
    return work(tx);
  }) as Promise<T>;
}
