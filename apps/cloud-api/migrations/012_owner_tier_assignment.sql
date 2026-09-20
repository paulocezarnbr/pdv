-- Funcionário e Dono só podem ser atribuídos por um proprietário ativo.
-- O trigger cobre painel, importação e sync, não apenas a interface do caixa.
CREATE OR REPLACE FUNCTION require_owner_for_protected_tier()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM discount_tiers t
        WHERE t.id = NEW.tier_id
          AND t.tenant_id = NEW.tenant_id
          AND t.code IN ('employee', 'owner')
    ) AND NOT EXISTS (
        SELECT 1 FROM users u
        WHERE u.id = NEW.assigned_by_user_id
          AND u.tenant_id = NEW.tenant_id
          AND u.role = 'owner'
          AND u.is_active = TRUE
    ) THEN
        RAISE EXCEPTION 'protected discount tier requires owner'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_protected_tier_requires_owner_insert
BEFORE INSERT ON customer_discount_tiers
FOR EACH ROW
EXECUTE FUNCTION require_owner_for_protected_tier();
CREATE TRIGGER trg_protected_tier_requires_owner_update
BEFORE UPDATE OF tier_id, assigned_by_user_id ON customer_discount_tiers
FOR EACH ROW
EXECUTE FUNCTION require_owner_for_protected_tier();
