#!/bin/sh
# Subida do contêiner: migra e só então atende.
#
# A ordem importa. Se o servidor subisse antes da migration, o healthcheck
# ficaria verde, o Coolify promoveria o contêiner, e a primeira requisicao do
# terminal falharia contra um schema velho — em producao, com o caixa aberto.
#
# Se a migration falhar, o contêiner morre. Isso e o comportamento certo no
# Coolify: o deploy nao e promovido, a versao anterior continua no ar, e o log
# da tentativa diz o que aconteceu.

set -e

echo "[entrypoint] aplicando migrations..."
node --experimental-strip-types scripts/migrate.ts

# Primeiro acesso sem terminal no contêiner: com BOOTSTRAP_EMAIL definido, cria
# tenant, loja e dono se o e-mail ainda não existir (o script é idempotente e
# nunca troca a senha de quem já existe). A senha vem de BOOTSTRAP_PASSWORD e
# não é impressa. Depois do primeiro acesso, apague as BOOTSTRAP_* no Coolify:
# o hash fica no banco, a senha não precisa ficar em lugar nenhum.
if [ -n "${BOOTSTRAP_EMAIL:-}" ]; then
  echo "[entrypoint] conferindo o primeiro acesso (${BOOTSTRAP_EMAIL})..."
  DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" \
    node --experimental-strip-types scripts/seed-tenant.ts \
      --tenant "${BOOTSTRAP_TENANT:-Loja de teste}" \
      --store "${BOOTSTRAP_STORE:-${BOOTSTRAP_TENANT:-Loja de teste}}" \
      --email "$BOOTSTRAP_EMAIL" \
      --name "${BOOTSTRAP_NAME:-Administrador}" \
      --password-env BOOTSTRAP_PASSWORD
fi

echo "[entrypoint] subindo o servidor na porta ${PORT:-3000}"
exec node server.js
