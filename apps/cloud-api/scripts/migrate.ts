/**
 * Aplica as migrations, em ordem, uma vez cada.
 *
 * Roda na subida do contêiner (ver `docker-entrypoint.sh`), e não como uma
 * etapa manual que alguém executa depois do deploy. O motivo é operacional: no
 * Coolify o deploy é um clique, e uma migration que depende de alguém lembrar
 * de rodá-la vira a API nova falando com o schema velho — em produção, com o
 * terminal já sincronizando.
 *
 * Duas travas fazem isso ser seguro com mais de uma réplica:
 *
 * 1. **Lock de aplicação no Postgres.** Dois contêineres subindo ao mesmo
 *    tempo pegariam o mesmo arquivo e o aplicariam duas vezes; o
 *    `pg_advisory_lock` faz o segundo esperar e, quando entra, o arquivo já
 *    está na tabela de controle.
 * 2. **Uma transação por arquivo.** Migration que falha no meio não deixa
 *    metade do schema aplicada — que é o estado do qual não se sai sem
 *    restaurar backup.
 */

import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import postgres from "postgres";

const MIGRATIONS_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "migrations");

//: Qualquer número fixo serve; ele só precisa ser o mesmo em todas as réplicas.
const LOCK_ID = 774_512_003;

/**
 * Dá login e senha ao papel `erp_app`, criado pela migration 004.
 *
 * Não vive no `.sql` porque a senha vem do ambiente e DDL não aceita
 * parâmetro. Montar a string na mão seria injeção de SQL com a senha do banco
 * como veículo — por isso o `format('%L')`, que é o escape do próprio
 * Postgres, aplicado a um valor que chega parametrizado.
 *
 * Sem `APP_DB_PASSWORD` o papel fica sem login e a aplicação segue conectando
 * como administrador. Isso **funciona** e é o caminho fácil, mas deixa o RLS
 * inerte: superusuário ignora política. O aviso abaixo e o `/api/health` dizem
 * isso em voz alta, porque uma barreira que não barra e ninguém sabe é pior
 * que barreira nenhuma.
 */
async function ensureAppRole(sql: postgres.Sql): Promise<void> {
  const password = process.env.APP_DB_PASSWORD;

  if (!password) {
    console.warn(
      "\n  AVISO: APP_DB_PASSWORD não definida.\n" +
        "  A aplicação vai conectar como administrador, e o isolamento por\n" +
        "  tenant no banco (RLS) fica INERTE — superusuário ignora política.\n" +
        "  O filtro por tenant da aplicação continua valendo; o que se perde é\n" +
        "  a segunda barreira. Ver COOLIFY.md, seção 3.\n",
    );
    return;
  }

  if (password.length < 12) {
    console.error("APP_DB_PASSWORD precisa de pelo menos 12 caracteres.");
    process.exit(1);
  }

  // Duas etapas, e a divisão é o ponto: o **Postgres** monta o comando, com o
  // `%L` do `format()` aplicado a um valor que chegou parametrizado, e só
  // então o texto já escapado é executado. Concatenar a senha em JavaScript
  // seria injeção de SQL tendo a senha do banco como veículo; e um parâmetro
  // direto no `ALTER ROLE` não funciona, porque DDL não aceita parâmetro.
  const [statement] = await sql<{ ddl: string }[]>`
    SELECT format('ALTER ROLE erp_app LOGIN PASSWORD %L', ${password}::text) AS ddl
  `;
  await sql.unsafe(statement!.ddl);
  console.log("  papel erp_app pronto para login.");
}

async function main(): Promise<void> {
  // As migrations criam tabela; a aplicação, não. Quando os dois papéis são
  // separados (ver migration 004), é `ADMIN_DATABASE_URL` que migra.
  const url = process.env.ADMIN_DATABASE_URL || process.env.DATABASE_URL;
  if (!url) {
    console.error("DATABASE_URL ausente. Ver .env.example.");
    process.exit(1);
  }

  const sql = postgres(url, { max: 1, onnotice: () => {} });

  try {
    await sql`
      CREATE TABLE IF NOT EXISTS schema_migrations (
        filename   TEXT PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )
    `;

    await sql`SELECT pg_advisory_lock(${LOCK_ID})`;
    try {
      const applied = new Set(
        (await sql<{ filename: string }[]>`SELECT filename FROM schema_migrations`).map(
          (row) => row.filename,
        ),
      );

      const files = (await readdir(MIGRATIONS_DIR))
        .filter((name) => name.endsWith(".sql"))
        // Ordem lexicográfica com prefixo numérico zero-padded: `010` vem
        // depois de `009`, que é o que se espera e o que `sort()` dá de graça
        // enquanto o prefixo tiver largura fixa.
        .sort();

      let count = 0;
      for (const file of files) {
        if (applied.has(file)) continue;

        const statements = await readFile(join(MIGRATIONS_DIR, file), "utf8");
        process.stdout.write(`  aplicando ${file}… `);

        await sql.begin(async (tx) => {
          await tx.unsafe(statements);
          await tx`INSERT INTO schema_migrations (filename) VALUES (${file})`;
        });

        process.stdout.write("ok\n");
        count += 1;
      }

      // Sempre, e não só quando alguma migration rodou: a senha pode ter sido
      // definida depois do primeiro deploy, e nesse caso não há migration nova
      // a aplicar — mas o papel precisa passar a aceitar login mesmo assim.
      await ensureAppRole(sql);

      console.log(
        count === 0
          ? "Banco já está na versão mais recente."
          : `${count} migration(s) aplicada(s).`,
      );
    } finally {
      await sql`SELECT pg_advisory_unlock(${LOCK_ID})`;
    }
  } catch (error) {
    console.error("\nMigration falhou:", error);
    process.exit(1);
  } finally {
    await sql.end({ timeout: 5 });
  }
}

void main();
