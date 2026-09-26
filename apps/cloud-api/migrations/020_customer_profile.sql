-- O cadastro do cliente (schema 15 do caixa, 26/09/2026).
--
-- A loja atende um condomínio: o morador fica ligado ao apartamento (e ao
-- bloco, quando o prédio tem mais de um), e o cadastro guarda como falar com a
-- pessoa. `phone` continua sendo o WhatsApp.
--
-- O CPF é único DENTRO de cada caixa, não aqui: os clientes não descem da
-- nuvem para os outros caixas, e a mesma pessoa cadastrada em dois balcões
-- chegaria duas vezes. Um índice único recusaria o segundo cadastro e, com
-- ele, travaria a sincronização daquele caixa. Juntar os cadastros repetidos
-- é trabalho do painel, com alguém olhando.
--
-- `marketing_opt_in_at` é a prova do consentimento (LGPD): a hora em que o
-- cliente disse sim no balcão. Sem ela, o sim é só uma coluna.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS cpf TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_resident BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS unit_block TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS unit_number TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS birth_date DATE;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS marketing_opt_in BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS marketing_opt_in_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_customers_cpf ON customers (tenant_id, cpf) WHERE cpf IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_unit
    ON customers (tenant_id, unit_block, unit_number) WHERE is_resident;
