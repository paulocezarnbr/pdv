/**
 * `PUT /api/panel/menu/products` — o que o cliente vê de um produto.
 *
 * Categoria, descrição e se aparece no cardápio. Preço NÃO muda por aqui: o
 * preço é do cadastro do caixa e da nota fiscal, e um segundo lugar para
 * alterá-lo faria o cardápio dizer um valor e o cupom cobrar outro.
 *
 * A categoria também desce para o caixa. Por isso a alteração avança o
 * `server_seq`: sem isso o próximo pull do terminal não veria a mudança.
 */

import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";
import { requireMenuEditor } from "@/lib/menu/admin";

export const dynamic = "force-dynamic";

const ProductSchema = z
  .object({
    product_id: z.string().uuid(),
    category: z.string().max(40).transform((value) => value.replace(/\s+/g, " ").trim()),
    description: z.string().max(280).transform((value) => value.trim()),
    menu_visible: z.boolean(),
  })
  .strict();

export const PUT = handler(async (request) => {
  const user = await requirePanelUser(request);
  requireMenuEditor(user);
  const body = await parseBody(request, ProductSchema);

  await withTenant(user.tenantId, async (tx) => {
    const [updated] = await tx<{ id: string }[]>`
      UPDATE products
         SET category = ${body.category || null},
             description = ${body.description || null},
             menu_visible = ${body.menu_visible},
             updated_at = now(),
             server_seq = nextval('server_seq_global')
       WHERE id = ${body.product_id} AND tenant_id = ${user.tenantId}
       RETURNING id::text
    `;
    if (!updated) throw new ApiError(404, "Produto não encontrado.");
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'menu_product_updated',
              ${tx.json({
                product_id: body.product_id, category: body.category,
                menu_visible: body.menu_visible,
              } as never)},
              ${clientIp(request)})
    `;
  });
  return json({ updated: body.product_id });
});
