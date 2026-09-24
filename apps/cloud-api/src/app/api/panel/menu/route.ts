/**
 * `GET/POST/DELETE /api/panel/menu` — os links do cardápio QR.
 *
 * Um link por loja (balcão, vitrine) ou por mesa. Revogar não apaga: o QR já
 * impresso passa a responder 404, e o link continua no histórico com a data.
 */

import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";
import { menuUrl, normalizeTableLabel, requireMenuEditor } from "@/lib/menu/admin";
import { newMenuToken } from "@/lib/menu/load";

export const dynamic = "force-dynamic";

const CreateSchema = z
  .object({
    store_id: z.string().uuid(),
    table_label: z.string().max(80).optional(),
  })
  .strict();

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  const result = await withTenant(user.tenantId, async (tx) => {
    const links = await tx<{
      id: string; store_id: string; store_name: string; token: string;
      table_label: string | null; created_at: string; revoked_at: string | null;
    }[]>`
      SELECT l.id::text, l.store_id::text, s.name AS store_name, l.token,
             l.table_label, l.created_at::text, l.revoked_at::text
        FROM menu_links l JOIN stores s ON s.id = l.store_id
       WHERE l.tenant_id = ${user.tenantId}
       ORDER BY l.revoked_at IS NOT NULL, s.name, l.table_label NULLS FIRST, l.created_at
    `;
    const products = await tx<{
      id: string; sku: string; name: string; price_cents: string; pricing_mode: string;
      category: string | null; description: string | null; menu_visible: boolean; is_active: boolean;
    }[]>`
      SELECT id::text, sku, name, price_cents::text, pricing_mode, category, description,
             menu_visible, is_active
        FROM products WHERE tenant_id = ${user.tenantId}
       ORDER BY category NULLS LAST, name
    `;
    return { links, products };
  });

  return json({
    links: result.links.map((link) => ({ ...link, url: menuUrl(request, link.token) })),
    products: result.products,
    can_edit: user.role === "owner" || user.role === "manager",
  });
});

export const POST = handler(async (request) => {
  const user = await requirePanelUser(request);
  requireMenuEditor(user);
  const body = await parseBody(request, CreateSchema);
  const tableLabel = normalizeTableLabel(body.table_label);

  const link = await withTenant(user.tenantId, async (tx) => {
    const [store] = await tx`SELECT 1 FROM stores WHERE id = ${body.store_id} AND tenant_id = ${user.tenantId}`;
    // 404 e não 403: a loja não existe *neste tenant*.
    if (!store) throw new ApiError(404, "Loja não encontrada.");
    const [created] = await tx<{ id: string; token: string }[]>`
      INSERT INTO menu_links (tenant_id, store_id, token, table_label, created_by)
      VALUES (${user.tenantId}, ${body.store_id}, ${newMenuToken()}, ${tableLabel}, ${user.id})
      RETURNING id::text, token
    `;
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'menu_link_created',
              ${tx.json({ link_id: created!.id, store_id: body.store_id, table_label: tableLabel } as never)},
              ${clientIp(request)})
    `;
    return created!;
  });

  return json({ id: link.id, url: menuUrl(request, link.token) }, { status: 201 });
});

export const DELETE = handler(async (request) => {
  const user = await requirePanelUser(request);
  requireMenuEditor(user);
  const id = new URL(request.url).searchParams.get("id") ?? "";
  if (!z.string().uuid().safeParse(id).success) throw new ApiError(400, "Link inválido.");

  await withTenant(user.tenantId, async (tx) => {
    const [revoked] = await tx<{ id: string }[]>`
      UPDATE menu_links SET revoked_at = now()
       WHERE id = ${id} AND tenant_id = ${user.tenantId} AND revoked_at IS NULL
       RETURNING id::text
    `;
    if (!revoked) throw new ApiError(404, "Link não encontrado ou já revogado.");
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'menu_link_revoked',
              ${tx.json({ link_id: id } as never)}, ${clientIp(request)})
    `;
  });
  return json({ revoked: id });
});
