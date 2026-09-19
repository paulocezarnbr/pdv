/**
 * Hash de senha do painel — isolado de propósito.
 *
 * Fica separado de `panel.ts` porque `panel.ts` importa o pool do Postgres no
 * topo do módulo, e importar o pool exige `DATABASE_URL` **e** `SESSION_SECRET`
 * na subida. O `seed-tenant.ts` precisa gerar um hash antes de existir qualquer
 * sessão, com só a URL do banco em mãos: sem esta separação, o script que cria
 * o primeiro usuário exigiria uma variável que só existe por causa das sessões
 * que ele ainda não pode abrir.
 *
 * Por que scrypt, e nao Argon2id
 * ------------------------------
 *
 * O PIN do PDV usa Argon2id, e la isso nao e negociavel: o hash fica num
 * SQLite **na maquina do caixa**, que o operador consegue abrir, entao o custo
 * por tentativa e a unica defesa contra um ataque offline com o banco em maos.
 *
 * Aqui o hash esta no Postgres da nuvem, atras da API. O cenario equivalente —
 * alguem com o dump do banco — ja e comprometimento total do servidor. O
 * scrypt e memory-hard, vem no runtime do Node e nao exige compilacao nativa:
 * numa imagem de conteiner que precisa subir rapido no Coolify, uma
 * dependencia nativa a menos e um modo de falha de build a menos. Os
 * parametros seguem a recomendacao da OWASP para scrypt (N=2^17, r=8, p=1).
 *
 * Se um dia o modelo de ameaca mudar — hash exposto por outra via —, a troca e
 * local: o formato guarda o algoritmo no proprio hash.
 */

import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";

// `promisify` perde a sobrecarga de quatro argumentos do `scrypt`, entao o
// tipo e declarado aqui. Sem isto nao da para passar `options` — e sem
// `options` nao da para subir o N nem o `maxmem`.
type ScryptOptions = { N: number; r: number; p: number; maxmem: number };
const scryptAsync = promisify(scrypt) as (
  password: string | Buffer,
  salt: string | Buffer,
  keylen: number,
  options: ScryptOptions,
) => Promise<Buffer>;

const SCRYPT = { N: 2 ** 17, r: 8, p: 1, keylen: 64 } as const;


export async function hashPassword(password: string): Promise<string> {
  const salt = randomBytes(16);
  const derived = (await scryptAsync(password, salt, SCRYPT.keylen, {
    N: SCRYPT.N,
    r: SCRYPT.r,
    p: SCRYPT.p,
    // O Node recusa N alto sem folga de memória; o padrão de 32 MiB não cobre
    // N=2^17. Sem esta linha o hash falha com "Invalid scrypt params" — só em
    // produção, no primeiro cadastro.
    maxmem: 256 * 1024 * 1024,
  }));

  // O formato carrega os parâmetros: subir o custo amanhã não invalida os
  // hashes de hoje, e dá para reidratar no próximo login de cada pessoa.
  return [
    "scrypt",
    SCRYPT.N,
    SCRYPT.r,
    SCRYPT.p,
    salt.toString("base64"),
    derived.toString("base64"),
  ].join("$");
}

export async function verifyPassword(stored: string, password: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 6 || parts[0] !== "scrypt") return false;

  const [, n, r, p, saltB64, hashB64] = parts as [
    string, string, string, string, string, string,
  ];
  try {
    const expected = Buffer.from(hashB64, "base64");
    const derived = (await scryptAsync(password, Buffer.from(saltB64, "base64"),
      expected.length, {
        N: Number(n),
        r: Number(r),
        p: Number(p),
        maxmem: 256 * 1024 * 1024,
      }));
    return derived.length === expected.length && timingSafeEqual(derived, expected);
  } catch {
    // Hash corrompido ou parâmetro absurdo nunca vira "autorizado".
    return false;
  }
}

