-- ===========================================================================
-- A saída da política parava de funcionar depois da primeira requisição
-- ===========================================================================
--
-- A política da migration 001 permite tudo quando `app.tenant_id` não está
-- definido. Essa saída existe para o que roda fora do contexto de uma
-- requisição: as próprias migrations, o `seed-tenant.ts`, e as rotas que
-- filtram por tenant no `WHERE` sem abrir transação.
--
-- O teste dela era `current_setting('app.tenant_id', true) IS NULL`. E aí está
-- o defeito: **no Postgres, um GUC customizado nunca volta a ser NULL depois
-- de ter sido definido uma vez naquela conexão.** O `SET LOCAL` é revertido no
-- fim da transação, mas revertido para o valor anterior — que, para um
-- parâmetro que nunca existiu no servidor, é a **string vazia**, não NULL.
--
--     BEGIN;  SELECT current_setting('app.tenant_id', true) IS NULL;  -- true
--             SELECT set_config('app.tenant_id', 'abc', true);
--     COMMIT;
--     BEGIN;  SELECT current_setting('app.tenant_id', true) IS NULL;  -- FALSE
--             SELECT current_setting('app.tenant_id', true);          -- ''
--     COMMIT;
--
-- O efeito prático era o pior tipo de bug. A conexão do pool que já tinha
-- atendido um `/sync/push` passava a ter `app.tenant_id = ''`, e `'' =
-- tenant_id::text` é falso para toda linha: a saída fechava e a política
-- bloqueava tudo. Qualquer rota que não definisse o tenant — `/commands/issue`
-- era uma — começava a falhar **conforme a conexão que o pool entregasse**.
-- Funciona no primeiro teste, funciona na máquina de quem escreveu, e falha
-- intermitentemente em produção.
--
-- Quem achou foi o teste de ponta a ponta contra a imagem Docker, e só depois
-- que o RLS deixou de ser inerte (migrations 003 e 004). Enquanto a aplicação
-- conectava como superusuário, o bug estava lá e não aparecia.
--
-- A correção trata vazio como ausente. E, independente dela, as rotas que
-- escrevem passaram a declarar o tenant explicitamente (`withTenant` em
-- `lib/db.ts`): a saída da política é uma conveniência para scripts, não algo
-- de que o caminho quente deva depender.

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
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING ('
            '  coalesce(current_setting(''app.tenant_id'', true), '''') = '''''
            '  OR tenant_id::text = current_setting(''app.tenant_id'', true)'
            ')', t);
    END LOOP;
END
$$;
