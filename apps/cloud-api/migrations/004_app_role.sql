-- ===========================================================================
-- Um papel sem privilégio para a aplicação usar
-- ===========================================================================
--
-- A migration 003 ligou `FORCE ROW LEVEL SECURITY` para que a política valesse
-- também para o dono das tabelas. O teste de integração mostrou que **isso
-- ainda não bastava**: o usuário que o Coolify cria no Postgres gerenciado é
-- `SUPERUSER`, e superusuário tem `rolbypassrls` — ele ignora RLS por
-- completo, com ou sem FORCE.
--
-- Ou seja: no arranjo padrão de um deploy de Coolify, com um usuário só, toda
-- a política de isolamento por tenant era decorativa. Ela aparecia no schema,
-- passava na revisão, e não barrava nada.
--
-- `erp_app` existe para ser o oposto disso: `NOSUPERUSER NOBYPASSRLS`, dono de
-- nada, com permissão apenas de ler e escrever nas tabelas que a aplicação usa.
-- Conectando por ele, a política passa a valer de verdade.
--
-- Quem migra e quem atende são papéis diferentes
-- ----------------------------------------------
--
-- As migrations continuam rodando com o usuário administrativo — elas criam
-- tabela, e `erp_app` não pode fazer isso, de propósito: uma aplicação que
-- consegue derrubar a tabela de auditoria não protege nada. Ver `ADMIN_DATABASE_URL`
-- em `.env.example`.
--
-- O `LOGIN` e a senha são definidos pelo `migrate.ts`, a partir de
-- `APP_DB_PASSWORD`. Sem essa variável o papel nasce sem login e a aplicação
-- segue conectando como administrador — com o RLS inerte, o que o
-- `/api/health` passa a **dizer em voz alta** em vez de esconder.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
        CREATE ROLE erp_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                            NOBYPASSRLS;
    ELSE
        -- Idempotente, e defensivo: se alguém tiver promovido o papel à mão,
        -- a migration o rebaixa de volta. Um `erp_app` com BYPASSRLS seria a
        -- política inerte de novo, agora sem ninguém desconfiar.
        ALTER ROLE erp_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO erp_app;

-- DML nas tabelas, e nada além. Sem DDL: a aplicação não altera schema.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO erp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO erp_app;

-- E o mesmo para o que as próximas migrations criarem: sem isto, a tabela nova
-- nasceria invisível para a aplicação, e o sintoma seria "permission denied"
-- na primeira requisição depois do deploy.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO erp_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO erp_app;

-- O ledger é o destino inalcançável do sistema inteiro: é ele que torna a
-- venda já sincronizada impossível de apagar por quem controla o PC da loja.
-- A aplicação **insere** e **lê**; não atualiza nem apaga. Sem esta revogação,
-- um bug de código — ou uma injeção que escape das listas brancas do
-- `SyncMerger` — alcançaria justamente o registro que não pode ser alcançado.
--
-- `device_anchors` **não** entra aqui, e a diferença importa: a âncora é
-- atualizada a cada elo novo (`ON CONFLICT DO UPDATE`), então revogar UPDATE
-- ali quebraria a sincronização no segundo evento de auditoria da loja. O que
-- protege a âncora é a regra da marca d'água alta no `SyncMerger`, que só
-- avança — nunca retrocede.
REVOKE UPDATE, DELETE ON audit_ledger FROM erp_app;
