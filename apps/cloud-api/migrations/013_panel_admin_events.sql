-- Auditoria imutável das mutações administrativas feitas por pessoas.
CREATE TABLE IF NOT EXISTS panel_admin_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    actor_user_id UUID NOT NULL REFERENCES panel_users(id),
    event_type TEXT NOT NULL,
    subject_user_id UUID,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_panel_admin_events_tenant_time
    ON panel_admin_events(tenant_id, created_at DESC);
ALTER TABLE panel_admin_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE panel_admin_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON panel_admin_events USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);

CREATE OR REPLACE FUNCTION prevent_panel_admin_event_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'panel admin events are immutable' USING ERRCODE='42501';
END;
$$;
CREATE TRIGGER trg_panel_admin_events_no_update
BEFORE UPDATE ON panel_admin_events FOR EACH ROW
EXECUTE FUNCTION prevent_panel_admin_event_mutation();
CREATE TRIGGER trg_panel_admin_events_no_delete
BEFORE DELETE ON panel_admin_events FOR EACH ROW
EXECUTE FUNCTION prevent_panel_admin_event_mutation();
REVOKE UPDATE, DELETE ON panel_admin_events FROM erp_app;

-- PostgreSQL historicamente concede CREATE no schema public a PUBLIC. Sem
-- retirar isso, um papel comprometido poderia criar objetos auxiliares mesmo
-- sem receber DDL explicitamente na migration 004.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
