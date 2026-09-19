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

echo "[entrypoint] subindo o servidor na porta ${PORT:-3000}"
exec node server.js
