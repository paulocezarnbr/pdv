-- Funcionário e Dono são vínculos permanentes. Esta barreira cobre inclusive
-- escrita direta, importação e sincronização que não passem pelo serviço do PDV.
CREATE OR REPLACE FUNCTION prevent_protected_discount_tier_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1
        FROM discount_tiers t
        WHERE t.id = OLD.tier_id
          AND t.tenant_id = OLD.tenant_id
          AND t.code IN ('employee', 'owner')
    ) THEN
        RAISE EXCEPTION 'protected discount tier cannot be removed'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.tier_id IS DISTINCT FROM NEW.tier_id AND EXISTS (
        SELECT 1
        FROM discount_tiers t
        WHERE t.id = OLD.tier_id
          AND t.tenant_id = OLD.tenant_id
          AND t.code IN ('employee', 'owner')
    ) THEN
        RAISE EXCEPTION 'protected discount tier cannot be changed'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_protected_discount_tier_no_change
    ON customer_discount_tiers;
DROP TRIGGER IF EXISTS trg_protected_discount_tier_no_delete
    ON customer_discount_tiers;
CREATE TRIGGER trg_protected_discount_tier_no_change
BEFORE UPDATE OF tier_id ON customer_discount_tiers
FOR EACH ROW
EXECUTE FUNCTION prevent_protected_discount_tier_change();
CREATE TRIGGER trg_protected_discount_tier_no_delete
BEFORE DELETE ON customer_discount_tiers
FOR EACH ROW
EXECUTE FUNCTION prevent_protected_discount_tier_change();
