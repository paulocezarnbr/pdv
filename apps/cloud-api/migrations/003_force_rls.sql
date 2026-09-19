-- ===========================================================================
-- O RLS precisa valer também para o dono das tabelas
-- ===========================================================================
--
-- A migration 001 ligou `ENABLE ROW LEVEL SECURITY` e criou a política de
-- isolamento por tenant. Isso **não bastava**, e o teste de integração é que
-- mostrou: no Postgres, o dono da tabela ignora RLS por padrão. Como a
-- aplicação se conecta com o mesmo usuário que rodou as migrations — que é o
-- arranjo normal num deploy de Coolify, com um usuário só —, a política estava
-- lá, ativa, e não barrava absolutamente nada.
--
-- Uma política que não barra é pior que política nenhuma: ela aparece no
-- schema, passa na revisão, e dá a quem lê a impressão de que existe uma
-- segunda barreira onde só existe o `WHERE tenant_id` da aplicação.
--
-- `FORCE ROW LEVEL SECURITY` faz a política valer também para o dono.
--
-- O que continua funcionando
-- --------------------------
--
-- A política tem uma saída deliberada: quando `app.tenant_id` não está
-- definido, ela permite tudo. É o que mantém de pé o que roda fora do contexto
-- de uma requisição — as próprias migrations, o `seed-tenant.ts`, e as rotas
-- que filtram por tenant no `WHERE` sem abrir transação (como o `/sync/pull`).
--
-- Isso significa que o RLS aqui é **segunda barreira**, não a primeira. Ele
-- cobre o esquecimento de um `WHERE` dentro de uma transação que já declarou
-- seu tenant — que é o caso do `/sync/push`, onde o dado de todos os clientes
-- passa. Não cobre uma consulta nova escrita fora de transação; para essa, a
-- primeira barreira continua sendo a aplicação, como sempre foi.

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'orders', 'order_items', 'order_item_ingredients', 'payments',
        'stock_movements', 'cash_sessions', 'audit_ledger', 'remote_commands',
        'fraud_alerts', 'products', 'inventory_items', 'recipes',
        'recipe_lines', 'users'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$$;
