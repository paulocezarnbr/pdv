/**
 * Cria o primeiro tenant, a primeira loja e o primeiro usuário do painel.
 *
 * As migrations criam o **schema**, não os dados. Sem este script, o primeiro
 * cliente exigiria alguém escrever INSERTs à mão no Postgres de produção — com
 * o hash da senha gerado onde? — que é exatamente o procedimento que produz um
 * `password_hash` em texto puro na primeira loja da vida do produto.
 *
 * A senha é gerada aqui e impressa **uma única vez**. Não é recuperável
 * depois, porque só o hash é guardado; recuperá-la exigiria guardá-la, e aí
 * ela não estaria protegida por nada.
 *
 * Uso:
 *     node --experimental-strip-types scripts/seed-tenant.ts \
 *       --tenant "Confeitaria Aurora" --store "Loja Centro" \
 *       --email dono@aurora.com.br
 *
 * Idempotente: rodar de novo com o mesmo e-mail não cria um segundo usuário
 * nem troca a senha de quem já existe. Rodar duas vezes por engano num
 * servidor de produção não pode derrubar o acesso do dono.
 *
 * `--password-env NOME`: a senha vem da variável de ambiente NOME e **não** é
 * impressa. É o caminho da subida do contêiner (`BOOTSTRAP_*` no
 * `docker-entrypoint.sh`): lá o que o script imprime vai para o log de deploy,
 * e senha em log de deploy é senha publicada para quem lê o painel do Coolify.
 */

import { randomBytes } from "node:crypto";

import postgres from "postgres";

import { hashPassword } from "../src/lib/auth/password.ts";

function arg(name: string): string | undefined {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

/**
 * Senha legível mas forte: 4 blocos de 5 caracteres de um alfabeto sem
 * ambiguidade visual. Quem recebe isto por telefone precisa conseguir ditá-la
 * sem errar `0` por `O`, e trocá-la no primeiro acesso.
 */
function generatePassword(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = randomBytes(20);
  const chars = [...bytes].map((byte) => alphabet[byte % alphabet.length]);
  return [0, 5, 10, 15].map((i) => chars.slice(i, i + 5).join("")).join("-");
}

async function main(): Promise<void> {
  const tenantName = arg("tenant");
  const storeName = arg("store") ?? tenantName;
  const email = arg("email");
  const userName = arg("name") ?? "Administrador";

  if (!tenantName || !email) {
    console.error(
      "uso: seed-tenant.ts --tenant <nome> [--store <nome>] --email <email> [--name <nome>]",
    );
    process.exit(1);
  }

  const url = process.env.DATABASE_URL;
  if (!url) {
    console.error("DATABASE_URL ausente.");
    process.exit(1);
  }

  const sql = postgres(url, { max: 1, onnotice: () => {} });

  try {
    const existing = await sql<{ id: string }[]>`
      SELECT id FROM panel_users WHERE lower(email) = ${email.toLowerCase()}
    `;
    if (existing[0]) {
      console.log(`Usuário ${email} já existe. Nada foi alterado.`);
      return;
    }

    const passwordEnv = arg("password-env");
    const given = passwordEnv ? process.env[passwordEnv] : undefined;
    if (passwordEnv && (!given || given.length < 12)) {
      console.error(`${passwordEnv} ausente ou com menos de 12 caracteres.`);
      process.exit(1);
    }
    const password = given ?? generatePassword();
    const passwordHash = await hashPassword(password);

    await sql.begin(async (tx) => {
      const [tenant] = await tx<{ id: string }[]>`
        INSERT INTO tenants (name) VALUES (${tenantName}) RETURNING id
      `;
      const [store] = await tx<{ id: string }[]>`
        INSERT INTO stores (tenant_id, name) VALUES (${tenant!.id}, ${storeName!})
        RETURNING id
      `;
      await tx`
        INSERT INTO panel_users
          (tenant_id, email, name, role, password_hash, can_authorize,
           max_discount_percent)
        VALUES (${tenant!.id}, ${email}, ${userName}, 'owner', ${passwordHash},
                TRUE, 100)
      `;

      console.log("");
      console.log("Tenant criado.");
      console.log(`  tenant_id : ${tenant!.id}`);
      console.log(`  store_id  : ${store!.id}`);
      console.log(`  e-mail    : ${email}`);
      if (given) {
        console.log(`  senha     : a de ${passwordEnv} (não impressa)`);
      } else {
        console.log(`  senha     : ${password}`);
        console.log("");
        console.log("A senha aparece UMA vez e não é recuperável.");
        console.log("Anote-a e troque no primeiro acesso.");
      }
      console.log("");
    });
  } catch (error) {
    console.error("Falhou:", error);
    process.exit(1);
  } finally {
    await sql.end({ timeout: 5 });
  }
}

void main();
