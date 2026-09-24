/**
 * `GET /api/panel/forecast?store=<id>` — a previsão da semana de uma loja.
 *
 * Leitura para qualquer perfil do painel: é a mesma informação de venda que o
 * resumo já mostra, só olhando para a frente. Sem `store`, a primeira loja do
 * restaurante — a previsão é por loja, porque cada uma tem o seu movimento.
 */

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { loadForecast } from "@/lib/forecast/load";
import { ApiError, handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  const wanted = new URL(request.url).searchParams.get("store");

  const result = await withTenant(user.tenantId, async (tx) => {
    const stores = await tx<{ id: string; name: string }[]>`
      SELECT id::text, name FROM stores WHERE tenant_id = ${user.tenantId} ORDER BY name
    `;
    const store = wanted ? stores.find((s) => s.id === wanted) : stores[0];
    if (!store) {
      if (wanted) throw new ApiError(404, "Loja não encontrada.");
      return null;
    }
    return { store, forecast: await loadForecast(tx, user.tenantId, store.id) };
  });

  return json({
    store: result?.store ?? null,
    forecast: result?.forecast ?? null,
    can_count: user.role === "owner" || user.role === "manager",
  });
});
