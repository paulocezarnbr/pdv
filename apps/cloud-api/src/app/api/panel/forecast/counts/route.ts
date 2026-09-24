/**
 * `POST /api/panel/forecast/counts` — lança a contagem de um insumo.
 *
 * É o que dá saldo à sugestão de compra. Dono e gerente; tudo em
 * `panel_admin_events`, porque uma contagem errada muda a compra da semana e
 * "quem contou" é a primeira pergunta quando falta farinha.
 */

import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { clearForecastCache } from "@/lib/forecast/load";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const CountSchema = z
  .object({
    store_id: z.string().uuid(),
    inventory_item_id: z.string().min(1).max(64),
    inventory_item_name: z.string().trim().min(1).max(120),
    // Em kg, como o painel mostra. Teto de 100 t: um erro de digitação com
    // três zeros a mais não vira sugestão de "não compre nada por um ano".
    counted_kg: z.number().finite().min(0).max(100_000),
  })
  .strict();

export const POST = handler(async (request) => {
  const user = await requirePanelUser(request);
  if (user.role !== "owner" && user.role !== "manager") {
    throw new ApiError(403, "Somente dono ou gerente lança contagem de estoque.");
  }
  const body = await parseBody(request, CountSchema);
  // Miligramas inteiros, como todo insumo do sistema: kg em ponto flutuante
  // acumularia erro de arredondamento no saldo.
  const countedMg = Math.round(body.counted_kg * 1_000_000);

  const count = await withTenant(user.tenantId, async (tx) => {
    const [store] = await tx`SELECT 1 FROM stores WHERE id = ${body.store_id} AND tenant_id = ${user.tenantId}`;
    if (!store) throw new ApiError(404, "Loja não encontrada.");
    const [created] = await tx<{ id: string; counted_at: Date }[]>`
      INSERT INTO inventory_counts
        (tenant_id, store_id, inventory_item_id, inventory_item_name, counted_mg, counted_by)
      VALUES (${user.tenantId}, ${body.store_id}, ${body.inventory_item_id},
              ${body.inventory_item_name}, ${countedMg}, ${user.id})
      RETURNING id::text, counted_at
    `;
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'inventory_counted',
              ${tx.json({
                count_id: created!.id, store_id: body.store_id,
                inventory_item_id: body.inventory_item_id, counted_mg: countedMg,
              } as never)},
              ${clientIp(request)})
    `;
    return created!;
  });

  clearForecastCache(user.tenantId, body.store_id);
  return json({ id: count.id, counted_at: new Date(count.counted_at).toISOString() }, { status: 201 });
});
