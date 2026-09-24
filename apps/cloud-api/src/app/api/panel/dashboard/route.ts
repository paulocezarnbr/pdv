import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { ApiError, handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

const MAX_RANGE_DAYS = 93;

function validDate(value: string | null, fallback: Date): Date {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new ApiError(400, "Período inválido.");
  return date;
}

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  const url = new URL(request.url);
  const now = new Date();
  const defaultFrom = new Date(now);
  defaultFrom.setUTCHours(0, 0, 0, 0);
  const from = validDate(url.searchParams.get("from"), defaultFrom);
  const to = validDate(url.searchParams.get("to"), now);
  const storeId = url.searchParams.get("store");

  if (to <= from || to.getTime() - from.getTime() > MAX_RANGE_DAYS * 86_400_000) {
    throw new ApiError(400, "Escolha um período entre 1 minuto e 93 dias.");
  }

  const result = await withTenant(user.tenantId, async (tx) => {
    const stores = await tx<{ id: string; name: string }[]>`
      SELECT id, name FROM stores
       WHERE tenant_id = ${user.tenantId}
       ORDER BY name
    `;
    if (storeId && !stores.some((store) => store.id === storeId)) {
      throw new ApiError(404, "Loja não encontrada.");
    }

    const storeFilter = storeId ? tx`AND o.store_id = ${storeId}` : tx``;
    const deviceStoreFilter = storeId ? tx`AND d.store_id = ${storeId}` : tx``;
    const alertStoreFilter = storeId ? tx`AND f.store_id = ${storeId}` : tx``;

    const [summary] = await tx<{
      revenue_cents: string;
      tips_cents: string;
      discount_cents: string;
      orders_count: string;
      avg_ticket_cents: string;
      cmv_cents: string;
    }[]>`
      WITH sold AS (
        SELECT o.id, o.total_cents, o.tip_cents, o.discount_cents
          FROM orders o
         WHERE o.tenant_id = ${user.tenantId}
           AND o.status = 'paid'
           AND o.closed_at >= ${from} AND o.closed_at < ${to}
           ${storeFilter}
      ), costs AS (
        SELECT coalesce(sum(i.unit_cost_cents), 0) AS cmv_cents
          FROM order_item_ingredients i
          JOIN order_items oi ON oi.id = i.order_item_id AND oi.tenant_id = i.tenant_id
          JOIN sold s ON s.id = oi.order_id
         WHERE i.tenant_id = ${user.tenantId} AND oi.canceled_at IS NULL
      )
      SELECT coalesce(sum(s.total_cents), 0) AS revenue_cents,
             coalesce(sum(s.tip_cents), 0) AS tips_cents,
             coalesce(sum(s.discount_cents), 0) AS discount_cents,
             count(*) AS orders_count,
             coalesce(avg(s.total_cents), 0)::bigint AS avg_ticket_cents,
             (SELECT cmv_cents FROM costs) AS cmv_cents
        FROM sold s
    `;

    const hourly = await tx<{ bucket: string; revenue_cents: string; orders_count: string }[]>`
      SELECT date_trunc('hour', o.closed_at)::text AS bucket,
             sum(o.total_cents) AS revenue_cents,
             count(*) AS orders_count
        FROM orders o
       WHERE o.tenant_id = ${user.tenantId} AND o.status = 'paid'
         AND o.closed_at >= ${from} AND o.closed_at < ${to}
         ${storeFilter}
       GROUP BY 1 ORDER BY 1
    `;

    const products = await tx<{
      name: string;
      quantity: string;
      revenue_cents: string;
    }[]>`
      SELECT oi.product_name AS name,
             count(*) AS quantity,
             sum(oi.total_cents) AS revenue_cents
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id
       WHERE o.tenant_id = ${user.tenantId} AND o.status = 'paid'
         AND o.closed_at >= ${from} AND o.closed_at < ${to}
         AND oi.canceled_at IS NULL ${storeFilter}
       GROUP BY oi.product_name
       ORDER BY revenue_cents DESC, name
       LIMIT 8
    `;

    const staff = await tx<{
      id: string;
      name: string;
      orders_count: string;
      revenue_cents: string;
      tips_cents: string;
    }[]>`
      SELECT coalesce(u.id::text, o.served_by_user_id, o.operator_id, 'sem-operador') AS id,
             coalesce(u.name, 'Sem operador identificado') AS name,
             count(*) AS orders_count,
             sum(o.total_cents) AS revenue_cents,
             sum(o.tip_cents) AS tips_cents
        FROM orders o
        LEFT JOIN users u ON u.tenant_id = o.tenant_id
          AND u.id::text = coalesce(o.served_by_user_id, o.operator_id)
       WHERE o.tenant_id = ${user.tenantId} AND o.status = 'paid'
         AND o.closed_at >= ${from} AND o.closed_at < ${to}
         ${storeFilter}
       GROUP BY 1, 2 ORDER BY revenue_cents DESC
       LIMIT 8
    `;

    const devices = await tx<{
      id: string;
      label: string;
      store_name: string;
      last_seen_at: string | null;
      pending_commands: string;
      awaiting_commands: string;
      open_alerts: string;
    }[]>`
      SELECT d.id, d.label, s.name AS store_name, d.last_seen_at::text,
             count(DISTINCT c.command_uuid) FILTER (WHERE c.status = 'pending') AS pending_commands,
             -- Subconjunto dos pendentes: esperam alguém no caixa. Separar é o
             -- que impede o gerente de ler "pendente" como "já vai".
             count(DISTINCT c.command_uuid) FILTER (
               WHERE c.status = 'pending' AND c.awaiting_confirmation_at IS NOT NULL
             ) AS awaiting_commands,
             count(DISTINCT f.id) FILTER (WHERE f.resolved_at IS NULL) AS open_alerts
        FROM devices d
        JOIN stores s ON s.id = d.store_id
        LEFT JOIN remote_commands c ON c.tenant_id = d.tenant_id AND c.device_id = d.id
        LEFT JOIN fraud_alerts f ON f.tenant_id = d.tenant_id AND f.device_id = d.id
       WHERE d.tenant_id = ${user.tenantId} AND d.revoked_at IS NULL
         ${deviceStoreFilter}
       GROUP BY d.id, d.label, s.name, d.last_seen_at
       ORDER BY s.name, d.label
    `;

    const alerts = await tx<{
      id: string;
      reason: string;
      store_name: string | null;
      device_id: string;
      raised_at: string;
    }[]>`
      SELECT f.id::text, f.reason, s.name AS store_name, f.device_id::text,
             f.raised_at::text
        FROM fraud_alerts f
        LEFT JOIN stores s ON s.id = f.store_id
       WHERE f.tenant_id = ${user.tenantId} AND f.resolved_at IS NULL
         ${alertStoreFilter}
       ORDER BY f.raised_at DESC LIMIT 10
    `;

    return { stores, summary, hourly, products, staff, devices, alerts };
  });

  return json({
    generatedAt: new Date().toISOString(),
    range: { from: from.toISOString(), to: to.toISOString() },
    ...result,
  });
});
