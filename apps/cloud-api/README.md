# Retaguarda — `@erpfood/cloud-api`

Next.js 15 (App Router) + Postgres. É o destino da sincronização dos terminais,
a origem dos cadastros e o único caminho pelo qual a nuvem comanda um PDV.

Para colocar no ar: **[COOLIFY.md](./COOLIFY.md)**.

---

## O que esta API é, e o que ela não é

O terminal é **offline-first** e continua vendendo com a internet fora. Esta
API não é o caminho crítico da venda; ela é o que torna a venda **inalcançável
depois**.

Quem controla o PC da loja controla o banco local. O que já chegou aqui, não:
a cadeia de auditoria é recalculada no servidor, ancorada por dispositivo, e
qualquer tentativa de reescrever um elo já ancorado é recusada e vira alerta.
É essa a garantia forte do sistema inteiro — não o hash no SQLite da loja.

---

## Rotas

| Rota | Quem chama | O que protege |
|---|---|---|
| `GET /api/health` | Coolify | Consulta o banco de verdade; 503 quando ele não responde. Publica `tenant_isolation`. |
| `POST /api/devices/activate` | Terminal | A **única** rota sem token — é ela que entrega o token. Código de uso único, consumo atômico, freio por IP. |
| `POST /api/sync/push` | Terminal | O faturamento de todas as lojas entra por aqui. Tenant do token, uma transação por lote, `Idempotency-Key` obrigatória. |
| `GET /api/sync/pull` | Terminal | Cadastros, por cursor de `server_seq`. Lista branca de tabelas. |
| `POST /api/commands/issue` | Painel | Comando assinado para um terminal. Teto por perfil e por janela. |
| `GET /api/commands/pending` | Terminal | Entrega **não** consome. |
| `POST /api/commands/results` | Terminal | O terminal conta o que fez. Só na primeira vez que conta. |
| `POST/DELETE /api/panel/session` | Painel | Login com scrypt, sessão em cookie `HttpOnly`, freio por e-mail **e** por IP. |

---

## Desenvolvimento

```bash
npm install
docker compose up -d postgres

cp .env.example .env
# edite DATABASE_URL e SESSION_SECRET

npm run migrate
npm run dev
```

## Testes

Três camadas, e cada uma pega uma classe de erro que a anterior não pega.

```bash
# 1. A regra, com o banco dublado. Rápido, roda em qualquer lugar.
npm test

# 2. A regra contra o Postgres: índices, ON CONFLICT, RLS, atomicidade.
docker run -d --name erp-pg-test -p 55432:5432 \
  -e POSTGRES_USER=erp -e POSTGRES_PASSWORD=erp -e POSTGRES_DB=erp \
  postgres:17-alpine
DATABASE_URL="postgres://erp:erp@localhost:55432/erp" \
  APP_DB_PASSWORD="senha-de-teste-do-app-12345" npm run migrate
TEST_DATABASE_URL="postgres://erp:erp@localhost:55432/erp" \
TEST_APP_DATABASE_URL="postgres://erp_app:senha-de-teste-do-app-12345@localhost:55432/erp" \
  npm test

# 3. O fluxo do terminal, pela imagem Docker, por HTTP. Ver scripts/e2e.py.
docker build -t erp-cloud-api:test .
# (sobe o contêiner — o cabeçalho de scripts/e2e.py tem o comando)
npm run e2e
```

**As três importam, e não é zelo excessivo.** Cada camada pegou um defeito real
que as outras não pegariam:

* a **(2)** mostrou que o RLS estava **inerte**: o usuário que o Coolify cria é
  superusuário, e superusuário ignora política, com ou sem `FORCE`. A política
  aparecia no schema, passava na revisão, e não barrava nada;
* a **(3)** mostrou que a saída da política parava de funcionar depois da
  primeira requisição — um GUC customizado no Postgres não volta a ser `NULL`,
  volta a ser string vazia. O sintoma era uma falha **intermitente**, conforme
  a conexão que o pool entregasse;
* a **(3)** também mostrou que o `next build` exigia `DATABASE_URL` só para
  fazer o parse da URL, o que obrigaria a passar a credencial de produção como
  argumento de build — onde ela ficaria gravada na imagem.

Nenhum dos três apareceu em revisão de código.

---

## O contrato com o terminal

O HMAC dos comandos existe **duas vezes**, em duas linguagens: aqui
(`src/lib/crypto/commands.ts`) e no PDV (`pdv/remote/protocol.py`). A
duplicação é intencional — o terminal precisa continuar conferindo mesmo quando
esta API é a parte comprometida.

O preço é que as duas podem divergir em algum detalhe de serialização, e a
divergência apareceria como "o painel parou de funcionar" numa sexta à noite.
Por isso `scripts/sign.ts` existe: o teste do desktop
(`tests/test_remote_transport.py`) **executa** os dois lados e compara byte a
byte. Quem mexer numa quebra o CI, não a loja.

---

## Estrutura

```
migrations/     SQL numerado, aplicado na subida do contêiner
scripts/
  migrate.ts    aplica as migrations (lock de aplicação + 1 transação por arquivo)
  seed-tenant.ts o primeiro tenant, loja e usuário
  sign.ts       ponte do teste de contrato com o PDV
  e2e.py        o fluxo do terminal, em Python — a linguagem do cliente real
src/
  app/api/      as rotas (nenhuma importa outra; o comum vive em lib/)
  lib/
    auth/       terminal (token), painel (sessão), senha (scrypt)
    crypto/     HMAC dos comandos e da cadeia de auditoria
    sync/       o SyncMerger e as quatro regras
    db.ts       pool preguiçoso, withTenant, estado do RLS
    env.ts      configuração validada no primeiro uso
tests/          vitest
```
