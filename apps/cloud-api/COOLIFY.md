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

Opcional, para o cardápio QR:

```
PUBLIC_BASE_URL=https://painel.minhaloja.com.br
```

É o endereço que vai impresso no QR das mesas. Sem ele, o painel usa o
endereço pelo qual foi aberto — o que dá certo no domínio do Coolify, e dá
errado se alguém gerar os QR acessando por IP ou por um domínio provisório:
o QR sairia impresso apontando para lá.

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
Host:     127.0.0.1
Path:     /api/health
Port:     3000
Interval: 10
Timeout:  5
Retries:  12
Start period: 120
```

**Host `127.0.0.1`, e não o padrão `localhost`.** O Coolify testa de dentro do
contêiner com `wget`, e no Alpine `localhost` resolve primeiro para `::1`
(IPv6). O Next escuta só em IPv4 (`HOSTNAME=0.0.0.0` no Dockerfile): com
`localhost`, a API sobe, imprime "Ready", e o healthcheck recebe "Connection
refused" até esgotar as tentativas — o Coolify desfaz o deploy de uma versão
que estava funcionando. Foi o que aconteceu no primeiro deploy de produção.

O `start period` de 120 s existe porque as migrations rodam antes do servidor
atender, e no primeiro deploy são todas de uma vez. Um valor curto derrubaria o
contêiner no meio de uma migration longa, deixando o schema pela metade — que é
o estado do qual não se sai sem restaurar backup.

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

### O IP do cliente atrás da Cloudflare

O limite de tentativas da ativação e do login conta por IP, e o app lê esse
IP de cabeçalhos (`src/lib/client-ip.ts`):

- **`CF-Connecting-IP`.** Vale só quando o salto mais próximo é uma borda da
  Cloudflare.
- **`X-Forwarded-For`.** Lido da direita para a esquerda, pulando a rede do
  Docker e as bordas da Cloudflare. O primeiro item, que o cliente escreve,
  nunca é usado sozinho.
- **Premissa.** O app nunca fica exposto sem o Traefik na frente. Com a DNS
  em modo proxy, feche a porta 443 da VPS para tudo que não for
  [faixa da Cloudflare](https://www.cloudflare.com/ips/). Assim, ninguém fala
  com o Traefik sem passar por ela.
- **Outro proxy no caminho** (balanceador, CDN própria). Declare as faixas
  dele, senão todos os clientes passam a dividir o IP do proxy no limite:

  ```env
  TRUSTED_PROXY_CIDRS=203.0.113.0/24,2001:db8::/32
  ```

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
DATABASE_URL="$ADMIN_DATABASE_URL" node --experimental-strip-types scripts/seed-tenant.ts \
  --tenant "Confeitaria Aurora" \
  --store "Loja Centro" \
  --email dono@aurora.com.br
```

O script imprime a senha gerada **uma única vez** — ela não é recuperável
depois, porque só o hash é guardado. Anote antes de fechar o terminal.

`DATABASE_URL="$ADMIN_DATABASE_URL"` porque a `DATABASE_URL` da aplicação é o
papel `erp_app`, preso ao RLS por tenant: ele não cria tenant nenhum.

### Sem acesso ao terminal: `BOOTSTRAP_*`

Defina as variáveis e faça um deploy. A subida do contêiner cria tenant, loja e
dono se o e-mail ainda não existir:

```
BOOTSTRAP_EMAIL=dono@aurora.com.br
BOOTSTRAP_PASSWORD=<12+ caracteres, marque "Is Literal">
BOOTSTRAP_TENANT=Confeitaria Aurora
BOOTSTRAP_STORE=Loja Centro
```

A senha **não** vai para o log: é você quem a definiu. Rodar de novo não cria
um segundo usuário nem troca a senha de quem já existe. Depois do primeiro
acesso, **apague as `BOOTSTRAP_*`**: o hash fica no banco, e a senha não
precisa ficar guardada em lugar nenhum.

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

### Captcha no login (Cloudflare Turnstile)

1. No painel da Cloudflare, abra **Turnstile → Add widget**:
   - informe o domínio do painel (ex.: `teste.rsrassessoria.com.br`);
   - escolha o modo **Managed**.
2. Copie as duas chaves para as variáveis da aplicação no Coolify e faça
   Redeploy:

   ```env
   TURNSTILE_SITE_KEY=<Site Key: pública, aparece na tela de login>
   TURNSTILE_SECRET_KEY=<Secret Key: marque "Is Literal"; nunca vai ao navegador>
   ```

Com a secreta definida, todo login precisa do token do widget. A chave vale
só para a ação `login` e é conferida com a Cloudflare antes do limite de
tentativas e antes da senha. Assim, um robô barrado não gasta tentativa da
conta nem o scrypt do servidor.

- **Sem a secreta:** o login funciona como antes.
- **Só com a secreta:** a tela avisa que falta a `TURNSTILE_SITE_KEY`.
- **Cloudflare fora do ar:** o login é recusado (503). É uma falha fechada
  de propósito, porque aceitar "porque não deu para conferir" deixaria o
  captcha ser contornado.

Para testar sem domínio, use as chaves de teste públicas da Cloudflare.
Elas sempre aprovam e mostram a faixa "Somente para teste":

- site key: `1x00000000000000000000AA`
- secret key: `1x0000000000000000000000000000000AA`

### Endereço da nuvem no terminal

No instalador do PDV, informe o endereço do domínio, como no navegador
(`https://api.seudominio.com.br`). O terminal acha a raiz `/api` sozinho;
digitar com `/api` no fim também funciona. Na ativação, o terminal entrega à
nuvem o segredo com que assina o ledger de auditoria: é com ele que a nuvem
confere cada venda que chega.

### Cadastro fiscal (NFC-e)

Logado como dono, a seção **Fiscal** do painel cadastra o emitente de cada loja
e o perfil tributário de cada produto, e lista o que ainda impede a primeira
nota. O **arquivo** do certificado A1, a senha e o CSC não entram no painel:
eles vão para o cofre do serviço fiscal, e o painel guarda só o nome da
referência (por exemplo, `loja-centro/a1.pfx`). A tela recusa qualquer campo
que pareça segredo.

A retaguarda sobe e o painel funciona **sem** o serviço fiscal.

**O serviço fiscal** é outra aplicação no mesmo projeto do Coolify:

| Campo | Valor |
|---|---|
| Build Pack | Dockerfile |
| Base Directory | `/apps/fiscal-net` |
| Porta | `8081`, só rede interna, **sem domínio** |
| Réplicas | **1**: o estado idempotente é um SQLite no volume |

Volumes:

- `/data`, persistente: o estado dos pedidos;
- `/run/secrets/fiscal`, somente leitura: o cofre.

No cofre ficam o A1 e a senha dele, em `loja-centro/a1.pfx` e
`loja-centro/a1.pfx.senha`. O CSC só é necessário com o QR Code v2.

Variáveis do serviço fiscal:

```
FISCAL_SERVICE_TOKEN=<token longo e aleatório; o mesmo da retaguarda>
FISCAL_PRODUCTION_ENABLED=false          # true só depois da homologação
FISCAL_QRCODE_VERSION=3                  # 2 volta ao QR com CSC
FISCAL_RESP_TEC_CNPJ=<CNPJ do responsável técnico, se a SEFAZ exigir>
FISCAL_RESP_TEC_CONTATO=<nome>
FISCAL_RESP_TEC_EMAIL=<e-mail>
FISCAL_RESP_TEC_FONE=<telefone só com dígitos>
```

Para emitir, cadastre também na retaguarda:

```
FISCAL_SERVICE_URL=http://<servico-fiscal-interno>:8081
FISCAL_SERVICE_TOKEN=<o mesmo token configurado no serviço fiscal>
```

`FISCAL_PRODUCTION_ENABLED=true` só depois da homologação na SEFAZ. Sem ela, a
tela recusa o ambiente de produção, e o `/api/health` mostra
`"fiscal": "somente homologação"`.

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

**O deploy fica preso em "unhealthy".** O Coolify mostra só isso, e às vezes
um "Return code: 1" — o motivo está nas **primeiras linhas** do log do
contêiner novo (em *Deployments*, abra a tentativa e role até "Container
logs"). O contêiner migra antes de atender, então a linha que importa é
quase sempre a primeira depois de `[entrypoint] aplicando migrations...`:

| Linha no log | Causa | O que fazer |
|---|---|---|
| `syntax error at or near "NULLS"` | Postgres 14 ou anterior com a migration 014 antiga. Corrigido: a 014 não usa mais sintaxe do 15. | Redeploy com a versão atual. Recomendado: Postgres 17. |
| `Migration falhou:` + outro erro SQL | O banco recusou uma migration. A transação volta inteira e o deploy anterior continua no ar. | Mande a mensagem completa; não edite o banco à mão. |
| `APP_DB_PASSWORD precisa de pelo menos 12 caracteres.` | Senha do papel `erp_app` curta. | Gere com `openssl rand -base64 24` e atualize a mesma senha em `DATABASE_URL`. |
| `DATABASE_URL ausente` | Nem `ADMIN_DATABASE_URL` nem `DATABASE_URL` chegaram ao contêiner. | Cadastre as variáveis (seção 3). |
| `[health] variáveis obrigatórias ausentes: [ 'SESSION_SECRET' ]` | Sobe, mas o health responde 503. | Cadastre `SESSION_SECRET`. |
| `[health] banco indisponível` | Host do banco inacessível daqui. | Ver abaixo. |
| `ECONNREFUSED` / `getaddrinfo ENOTFOUND` na migration | Host errado, ou banco e aplicação em redes diferentes do Coolify. | Use a URL **interna** do recurso e confira se os dois estão no mesmo projeto/servidor. |

A retaguarda migra do PostgreSQL **14 ao 17** — as quinze migrations são
testadas nas quatro versões.

Quando a causa é `DATABASE_URL`, confira no Terminal do recurso:

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
