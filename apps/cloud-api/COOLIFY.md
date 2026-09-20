# Deploy no Coolify

Passo a passo, na ordem. Cada etapa diz **por que** ela existe — se algo der
errado, é mais rápido saber o que aquela etapa garantia do que refazer tudo.

---

## 1. O Postgres primeiro

No projeto do Coolify: **+ New** → **Database** → **PostgreSQL 17**.

Crie o banco **antes** da aplicação. A imagem roda as migrations na subida
(`docker-entrypoint.sh`); sem banco, o contêiner morre no primeiro segundo e o
log mostra falha de conexão — que é o comportamento certo, mas custa um deploy
inteiro para descobrir.

Guarde a **connection string interna** que o Coolify mostra. É ela que vai em
`DATABASE_URL`. A interna, não a pública: o tráfego entre a API e o banco não
precisa sair do servidor, e publicar a porta do Postgres na internet é abrir
para o mundo o banco que tem o faturamento de todos os clientes.

> Não use o `docker-compose.yml` deste diretório em produção. Ele é para
> desenvolvimento local. Um Postgres subido pelo compose no servidor de
> produção significa cuidar de backup na mão — e descobrir que ninguém cuidou
> no dia em que ele for preciso. O recurso gerenciado do Coolify tem backup
> agendado e restauração de um clique.

---

## 2. A aplicação

**+ New** → **Application** → o repositório Git.

| Campo | Valor | Por quê |
|---|---|---|
| Build Pack | **Dockerfile** | O `Dockerfile` deste diretório já faz build em três estágios e roda as migrations na subida. O Nixpacks adivinharia o processo e perderia as duas coisas. |
| Base Directory | `/apps/cloud-api` | O repositório é um monorepo; sem isto o Coolify procura o `Dockerfile` na raiz. |
| Dockerfile Location | `/apps/cloud-api/Dockerfile` | |
| Port | `3000` | A imagem expõe 3000 e respeita `PORT` se o Coolify injetar outra. |
| Health Check Path | `/api/health` | Ver abaixo. |

---

## 3. Variáveis de ambiente

Em **Environment Variables**:

```
ADMIN_DATABASE_URL=postgres://<usuario>:<senha>@<host-interno>:5432/<banco>
APP_DB_PASSWORD=<gere: openssl rand -base64 24>
DATABASE_URL=postgres://erp_app:<a mesma APP_DB_PASSWORD>@<host-interno>:5432/<banco>
SESSION_SECRET=<gere: openssl rand -base64 48>
APP_VERSION=1.0.0
```

`PORT` o Coolify injeta. **Não cadastre.**

### Por que duas conexões

`ADMIN_DATABASE_URL` é a string que o Coolify te deu. Ela **migra**: cria
tabela, índice, política.

`DATABASE_URL` é a que a **aplicação** usa, e aponta para `erp_app` — um papel
que as migrations criam, sem privilégio de DDL e, o que mais importa,
**sem superusuário**.

Essa separação não é preciosismo. O usuário que o Postgres gerenciado do
Coolify cria é `SUPERUSER`, e superusuário tem `rolbypassrls`: ele **ignora
toda política de RLS**, com ou sem `FORCE`. Conectando a aplicação com ele,
o isolamento por tenant no banco fica decorativo — aparece no schema, passa na
revisão, e não barra nada. Isso foi descoberto por um teste de integração, não
por leitura de código, e é o tipo de falha que só aparece quando alguém já
vazou dados de um cliente para outro.

Com `erp_app`, a política passa a valer de verdade, e a aplicação perde o poder
de alterar ou apagar o ledger de auditoria — que é o registro que o sistema
inteiro existe para tornar inalcançável.

### Se você não quiser separar agora

Funciona: deixe `APP_DB_PASSWORD` vazia e ponha a string do Coolify em
`DATABASE_URL`. A aplicação sobe, sincroniza e atende normalmente — o filtro
por tenant de cada consulta continua valendo, e ele é a primeira barreira.

O que você perde é a segunda barreira, para o dia em que alguém escrever uma
consulta nova e esquecer o `WHERE tenant_id`. O `/api/health` passa a responder
`"tenant_isolation":"inerte"`, e o log do deploy avisa. Nada disso fica
escondido, de propósito.

### Falhar cedo

`DATABASE_URL` e `SESSION_SECRET` são obrigatórias e o processo **recusa
subir** sem elas. É deliberado: variável ausente virando `undefined` produziria
um `WHERE tenant_id = NULL` que não casa com nada, e o erro apareceria horas
depois como "o terminal parou de sincronizar", sem nada ligando uma coisa à
outra. Faltando uma, o healthcheck nunca fica verde, o deploy anterior continua
no ar, e o log da tentativa diz qual variável falta.

---

## 4. O healthcheck não é formalidade

`/api/health` **consulta o banco** e responde **503** quando ele não responde.

No Coolify, o deploy só promove o contêiner novo depois que o healthcheck fica
verde. Um `/health` que devolvesse 200 sem olhar o banco faria o Coolify
promover uma API que sobe, atende e responde 500 em toda rota — derrubando a
versão anterior, que funcionava, para colocar no ar uma que não funciona.

Configure em **Health Check**:

```
Path:     /api/health
Interval: 30
Timeout:  5
Retries:  3
Start period: 40
```

O `start period` de 40 s existe porque as migrations rodam antes do servidor
atender. Um valor curto derrubaria o contêiner no meio de uma migration longa,
deixando o schema pela metade — que é o estado do qual não se sai sem restaurar
backup.

---

## 5. Domínio e HTTPS

Em **Domains**, aponte o subdomínio (ex.: `api.seudominio.com.br`). O Coolify
emite o certificado Let's Encrypt e renova sozinho.

O HTTPS aqui **não é opcional**. Por esta API trafegam o token de
sincronização, o segredo HMAC do ledger na ativação, e os comandos assinados do
painel. Em HTTP, qualquer intermediário no caminho lê tudo isso — e a proteção
construída dos dois lados (assinatura, âncora, freio) é contornada por quem
simplesmente escuta.

Ligue também **Force HTTPS**.

---

## 6. Primeiro deploy

**Deploy**. Acompanhe o log:

```
[entrypoint] aplicando migrations...
  aplicando 001_init.sql… ok
  aplicando 002_panel_throttle.sql… ok
  aplicando 003_force_rls.sql… ok
  aplicando 004_app_role.sql… ok
  papel erp_app pronto para login.
4 migration(s) aplicada(s).
[entrypoint] subindo o servidor na porta 3000
```

Depois, confira de fora:

```bash
curl https://api.seudominio.com.br/api/health
```

```json
{
  "service": "erpfood-cloud-api",
  "version": "1.0.0",
  "uptime_seconds": 12,
  "database": "ok",
  "tenant_isolation": "ativo"
}
```

`"database":"ok"` é o que decide o 200 ou o 503.

`"tenant_isolation"` diz se a segunda barreira está valendo. `ativo` é o
esperado; `inerte` significa que a aplicação está conectando como superusuário
e a política de RLS não barra nada — ver a seção 3. Não derruba o healthcheck,
porque o sistema funciona assim; fica à vista para não ser esquecido.

---

## 7. O primeiro tenant

As migrations criam o schema, não os dados. Para o primeiro cliente, rode uma
vez no **Terminal** do recurso da aplicação no Coolify:

```bash
node --experimental-strip-types scripts/seed-tenant.ts \
  --tenant "Confeitaria Aurora" \
  --store "Loja Centro" \
  --email dono@aurora.com.br
```

O script imprime a senha gerada **uma única vez** — ela não é recuperável
depois, porque só o hash é guardado. Anote antes de fechar o terminal.

### Entrar no painel

Abra a raiz do domínio configurado no Coolify, por exemplo:

```text
https://api.seudominio.com.br/
```

Use o e-mail e a senha criados pelo script. O cookie da sessão é `HttpOnly`,
`SameSite=Lax` e `Secure`; por isso o painel deve ser acessado por **HTTPS**.
Em HTTP puro o navegador recebe a sessão, mas corretamente se recusa a
reenviá-la. O proxy do Coolify cuida do certificado público e da renovação.

O painel consolida todas as lojas do tenant e permite filtrar uma loja. A API
valida esse filtro contra o tenant da sessão; trocar o UUID na URL não permite
consultar outra empresa. Os números são atualizados automaticamente a cada
30 segundos, e o horário do último dado fica sempre visível.

---

## Atualizar

`git push` na branch configurada. Com **Automatic Deployment** ligado, o
Coolify constrói a imagem nova, sobe o contêiner, espera o healthcheck e só
então troca o tráfego. Migrations novas entram sozinhas, na subida.

Se a migration falhar, o contêiner morre e o Coolify mantém a versão anterior
no ar. É o comportamento certo: melhor continuar na versão antiga do que servir
uma API nova contra um schema pela metade.

---

## Quanto custa de máquina

Para até ~50 lojas sincronizando:

| Recurso | Mínimo | Confortável |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 2 GB | 4 GB |
| Disco | 20 GB | 40 GB |

O gargalo é o Postgres, não a API: o `push` é dominado por escrita, e o ledger
de auditoria nunca é apagado — é justamente o registro que não pode sumir. Um
disco que cresce alguns GB por ano por loja é o esperado, não um vazamento.

---

## Diagnóstico

**`permission denied for table ...` no primeiro deploy.** `DATABASE_URL` está
apontando para `erp_app`, mas `ADMIN_DATABASE_URL` não foi cadastrada — então
as migrations tentaram criar tabela com o papel que não pode. Cadastre a
`ADMIN_DATABASE_URL` e redeploy.

**`password authentication failed for user "erp_app"`.** A senha em
`DATABASE_URL` não bate com `APP_DB_PASSWORD`. As duas precisam ser idênticas:
as migrations definem a senha do papel a partir de `APP_DB_PASSWORD`, e a
aplicação se conecta com a que está na URL.

**`/api/health` responde `"tenant_isolation":"inerte"`.** A aplicação está
conectando como superusuário. Ver a seção 3 — funciona, mas sem a segunda
barreira.

**O deploy fica preso em "unhealthy".** O healthcheck está falhando, e quase
sempre é `DATABASE_URL`. Confira no Terminal do recurso:

```bash
node -e "console.log(process.env.DATABASE_URL?.replace(/:[^:@]+@/, ':***@'))"
```

Se aparecer `undefined`, a variável não chegou. Se aparecer o host **público**
do Postgres, troque pelo interno.

**O terminal responde 401 em tudo.** O token de sincronização não bate. O
terminal precisa ser ativado de novo pelo painel — o token é guardado por hash
e não há como recuperá-lo, por construção.

**O terminal responde 403 "Identidade do terminal não confere".** O corpo da
requisição traz um `tenant_id` ou `device_id` diferente do token. Isso é sinal
de terminal clonado, e a recusa é o comportamento correto: ver o cabeçalho de
`api/sync/push/route.ts`.

**"Comando não suportado".** A lista de comandos é curta de propósito — cada
item nela é uma permissão nova dada a quem comprometer o painel. Ver
`lib/crypto/commands.ts`.
