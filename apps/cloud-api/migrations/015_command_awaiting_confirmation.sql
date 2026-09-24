-- ===========================================================================
-- Fase 3.5.b — comando remoto que espera aceite de alguém no caixa
-- ===========================================================================
--
-- Cancelar pelo painel um item que já foi para a cozinha não vale sem alguém
-- na loja concordando (ver `remote/commands.py`, trava 7, no desktop). Até o
-- aceite, o terminal mantém o comando `pending` — ele não foi decidido, e a
-- entrega continua. Sem estas colunas o painel mostraria só "entregue", que o
-- gerente lê como "já vai", e ele iria embora sem saber que falta uma pessoa.
--
-- Colunas, e não um quarto status: o `CHECK` de `status` é lido pelo relato
-- final, que só aceita sair de `pending` uma vez. Um status intermediário
-- obrigaria aquela trava a aceitar duas origens, e é essa a trava que impede
-- um terminal comprometido de reescrever o registro de um cancelamento que ele
-- mesmo aplicou.
ALTER TABLE remote_commands
    ADD COLUMN IF NOT EXISTS awaiting_confirmation_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS awaiting_message TEXT;
