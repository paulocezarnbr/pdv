import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { hashPin, validatePin } from "@/lib/auth/pin";
import { withTenant } from "@/lib/db";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const OwnerSchema = z.object({
  name: z.string().trim().min(3).max(120),
  login: z.string().trim().toLowerCase().regex(/^[a-z0-9._-]{3,40}$/),
  pin: z.string().min(6).max(12),
});

function requireOwner(role: string): void {
  if (role !== "owner") {
    throw new ApiError(403, "Somente outro proprietário pode cadastrar um Dono.");
  }
}

export const GET = handler(async (request) => {
  const actor = await requirePanelUser(request);
  requireOwner(actor.role);
  const owners = await withTenant(actor.tenantId, (tx) => tx<{
    id: string; name: string; login: string; is_active: boolean; updated_at: string;
  }[]>`
    SELECT id, name, login, is_active, updated_at::text
      FROM users
     WHERE tenant_id=${actor.tenantId} AND role='owner'
     ORDER BY name
  `);
  return json({ owners });
});

export const POST = handler(async (request) => {
  const actor = await requirePanelUser(request);
  requireOwner(actor.role);
  const body = await parseBody(request, OwnerSchema);
  try {
    validatePin(body.pin);
  } catch (error) {
    throw new ApiError(422, error instanceof Error ? error.message : "PIN inválido.");
  }
  const pinHash = await hashPin(body.pin);

  const created = await withTenant(actor.tenantId, async (tx) => {
    const rows = await tx<{ id: string; name: string; login: string }[]>`
      INSERT INTO users
        (tenant_id,name,login,role,pin_hash,can_authorize,
         max_discount_percent,is_active,updated_at)
      VALUES (${actor.tenantId},${body.name},${body.login},'owner',${pinHash},
              TRUE,100,TRUE,now())
      ON CONFLICT DO NOTHING
      RETURNING id,name,login
    `;
    const owner = rows[0];
    if (!owner) throw new ApiError(409, "Este login já está cadastrado.");
    await tx`
      INSERT INTO panel_admin_events
        (tenant_id,actor_user_id,event_type,subject_user_id,payload_json,ip)
      VALUES (${actor.tenantId},${actor.id},'owner_created',${owner.id},
              ${tx.json({ name: owner.name, login: owner.login } as never)},
              ${clientIp(request)})
    `;
    return owner;
  });

  return json({ owner: created }, { status: 201 });
});
