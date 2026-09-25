# =============================================================================
# Imagem da retaguarda — feita para o Coolify
# =============================================================================
#
# Três estágios, e cada um existe por um motivo concreto:
#
# 1. `deps`    — instala dependências sozinho, numa camada que só é invalidada
#                quando o `package-lock.json` muda. Sem separar, cada alteração
#                de uma linha de código reinstalaria o `node_modules` inteiro, e
#                um deploy de dois minutos viraria dez.
# 2. `builder` — compila o Next. O `output: "standalone"` faz o Next copiar
#                para `.next/standalone` apenas o runtime e as dependências
#                efetivamente alcançadas.
# 3. `runner`  — só o que roda. Não tem compilador, não tem `devDependencies` e
#                não tem o código-fonte: o que não está na imagem não pode ser
#                explorado nem vazado.
#
# O resultado fica na casa de 200 MB em vez de 1,5 GB — que no Coolify é a
# diferença entre um deploy que termina e um que estoura o disco do servidor
# depois de algumas versões.

# -----------------------------------------------------------------------------
FROM node:22-alpine AS deps

WORKDIR /app

# `libc6-compat` porque alguns binários pré-compilados do ecossistema Node
# assumem glibc; no Alpine (musl) eles falham com "Error loading shared library"
# — um erro que aparece só em produção, porque em desenvolvimento ninguém usa
# Alpine.
RUN apk add --no-cache libc6-compat

COPY package.json package-lock.json* ./
# `npm ci` e não `npm install`: instala EXATAMENTE o lock, e falha se o lock
# estiver dessincronizado do package.json. Um `install` silenciosamente
# atualizaria uma transitiva e colocaria em produção um código que ninguém
# testou.
RUN npm ci --no-audit --no-fund

# -----------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# O build NÃO fala com o banco: toda rota é `force-dynamic`, então o Next não
# tenta pré-renderizar nada. Isso é deliberado — se o build precisasse de
# `DATABASE_URL`, o Coolify teria de receber a credencial de produção como
# argumento de build, onde ela ficaria gravada na imagem.
RUN npm run build

# -----------------------------------------------------------------------------
FROM node:22-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

RUN apk add --no-cache libc6-compat

# Usuário sem privilégio. Um processo de aplicação nunca precisa ser root, e a
# imagem oficial do Node já traz o usuário `node` pronto — criar outro só
# acrescentaria uma linha e nenhuma segurança.
USER node

# O standalone traz o `server.js` e o `node_modules` mínimo. `static` e
# `public` ficam de fora dele de propósito (o Next assume um CDN na frente),
# então são copiados à parte.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

# As migrations viajam junto e rodam na subida: no Coolify o deploy é um
# clique, e uma migration que depende de alguém lembrar de rodá-la vira a API
# nova falando com o schema velho — em produção, com o terminal sincronizando.
COPY --from=builder --chown=node:node /app/migrations ./migrations
COPY --from=builder --chown=node:node /app/scripts ./scripts
# O `seed-tenant.ts` importa o hash de senha do painel. Sem este arquivo o
# primeiro tenant não tinha como ser criado dentro do contêiner — a imagem não
# leva `src/`. Ele só usa `node:crypto`: nada mais do código-fonte vem junto.
COPY --from=builder --chown=node:node /app/src/lib/auth/password.ts ./src/lib/auth/password.ts
COPY --from=builder --chown=node:node /app/node_modules/postgres ./node_modules/postgres
COPY --chown=node:node docker-entrypoint.sh ./docker-entrypoint.sh

EXPOSE 3000

# O healthcheck consulta o banco (ver `api/health/route.ts`): no Coolify é ele
# que decide se o contêiner novo assume o tráfego. Um healthcheck que só
# confirma "a porta abriu" promoveria uma API que responde 500 em toda rota,
# derrubando a versão anterior — que funcionava.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/bin/sh", "./docker-entrypoint.sh"]
