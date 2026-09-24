-- Estado operacional de cada terminal, como ELE o relata.
--
-- `devices.last_seen_at` dizia só "falou comigo há pouco". Não dizia o que
-- importa quando a loja para de sincronizar: quantas vendas estão presas na
-- fila, há quanto tempo, quantas foram para a quarentena e por quê, e se o
-- relógio do caixa está certo. O defeito que deixou nenhum terminal real
-- sincronizar passaria semanas sem ninguém ver — o painel mostrava o terminal
-- "online" e o faturamento vazio.
--
-- Uma linha por terminal, sobrescrita a cada ciclo: é fotografia, não
-- histórico. O relógio do terminal é guardado junto do da nuvem, e o desvio é
-- calculado AQUI: o caixa não é testemunha confiável da própria hora.
CREATE TABLE IF NOT EXISTS device_telemetry (
    device_id              UUID PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    store_id               UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    reported_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    terminal_clock         TIMESTAMPTZ NOT NULL,
    clock_drift_ms         BIGINT NOT NULL,
    pending_items          INTEGER NOT NULL CHECK (pending_items >= 0),
    quarantined_items      INTEGER NOT NULL CHECK (quarantined_items >= 0),
    oldest_pending_at      TIMESTAMPTZ,
    last_quarantine_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_telemetry_tenant ON device_telemetry (tenant_id, store_id);

ALTER TABLE device_telemetry ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_telemetry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON device_telemetry;
CREATE POLICY tenant_isolation ON device_telemetry USING (
    coalesce(current_setting('app.tenant_id', true), '') = '' OR
    tenant_id::text = current_setting('app.tenant_id', true)
);
GRANT SELECT, INSERT, UPDATE ON device_telemetry TO erp_app;
