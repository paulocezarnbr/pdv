-- O cadastro do cliente é do ESTABELECIMENTO, não do caixa (pedido do dono,
-- 26/09/2026).
--
-- Até aqui o cliente só subia: cada caixa tinha os próprios clientes, e a
-- mesma pessoa cadastrada em dois balcões virava duas. Agora o cliente
-- pertence à loja (`store_id`) e desce para todos os caixas dela pelo
-- `/api/sync/pull`. O `store_id` vem do terminal que cadastrou, nunca do corpo.
--
-- O WhatsApp é único POR LOJA: a mesma pessoa pode ser cliente de duas lojas
-- da rede, com saldos separados, porque cashback, pré-pago e fiado já são por
-- loja.
--
-- Os cadastros antigos, sem loja, recebem a loja por dedução, e só quando ela
-- é certa:
--   1. a rede tem um estabelecimento só;
--   2. o cliente movimentou saldo (cashback, pré-pago ou fiado) numa loja só.
-- O que sobrar sem loja fica visível para todos os caixas da rede, como era.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS store_id UUID REFERENCES stores(id);

UPDATE customers c
   SET store_id = only_store.id
  FROM (SELECT tenant_id, (array_agg(id))[1] AS id FROM stores GROUP BY tenant_id HAVING count(*) = 1) only_store
 WHERE c.store_id IS NULL AND c.tenant_id = only_store.tenant_id;

UPDATE customers c
   SET store_id = moved.store_id
  FROM (
        SELECT tenant_id, customer_id, (array_agg(DISTINCT store_id))[1] AS store_id
          FROM (SELECT tenant_id, customer_id, store_id FROM cashback_ledger
                UNION ALL SELECT tenant_id, customer_id, store_id FROM prepaid_ledger
                UNION ALL SELECT tenant_id, customer_id, store_id FROM credit_account_ledger) movements
         GROUP BY tenant_id, customer_id
        HAVING count(DISTINCT store_id) = 1
       ) moved
 WHERE c.store_id IS NULL AND c.tenant_id = moved.tenant_id AND c.id = moved.customer_id;

DROP INDEX IF EXISTS idx_customers_phone;
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_store_phone
    ON customers (tenant_id, store_id, phone) WHERE phone IS NOT NULL;
-- A descida para os caixas pagina por server_seq dentro da loja.
CREATE INDEX IF NOT EXISTS idx_customers_store_seq ON customers (tenant_id, store_id, server_seq);
