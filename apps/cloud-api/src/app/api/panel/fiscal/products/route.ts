/**
 * `PUT /api/panel/fiscal/products` — o perfil tributário de um produto.
 *
 * Só o proprietário, pelo mesmo motivo do cadastro da loja: NCM, CFOP e CST
 * decidem quanto imposto a empresa recolhe em cada venda. O painel confere
 * forma e coerência (ver `lib/fiscal/registry.ts`); a escolha dos códigos é do
 * contador, e o sistema não a sugere.
 *
 * O perfil vale para as vendas **seguintes**. A nota já autorizada guardou os
 * códigos no próprio XML, e corrigir um cadastro hoje não reescreve a
 * escrituração de ontem — se ontem estava errado, a correção é fiscal (carta de
 * correção ou cancelamento), não de cadastro.
 */

import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { validateProductProfile } from "@/lib/fiscal/registry";
import { ApiError, clientIp, handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const optionalCode = z
  .string()
  .max(8)
  .nullish()
  .transform((value) => (value && value.trim() ? value.trim() : null));

const ProfileSchema = z
  .object({
    product_id: z.string().min(1).max(64),
    ncm: z.string().max(10).transform((value) => value.replace(/\D/g, "")),
    cfop: z.string().max(5).transform((value) => value.replace(/\D/g, "")),
    cest: optionalCode,
    unit_code: z.string().max(6).transform((value) => value.trim().toUpperCase()),
    origin: z.number().int(),
    csosn: optionalCode,
    cst_icms: optionalCode,
    cst_pis: z.string().max(2),
    cst_cofins: z.string().max(2),
  })
  .strict();

export const PUT = handler(async (request) => {
  const user = await requirePanelUser(request);
  if (user.role !== "owner") {
    throw new ApiError(403, "Somente o proprietário altera o perfil tributário.");
  }
  const body = await parseBody(request, ProfileSchema);

  const saved = await withTenant(user.tenantId, async (tx) => {
    const [product] = await tx<{ id: string; name: string }[]>`
      SELECT id::text AS id, name FROM products
       WHERE tenant_id=${user.tenantId} AND id::text=${body.product_id}`;
    if (!product) throw new ApiError(404, "Produto não encontrado.");

    const regimes = (await tx<{ tax_regime: number }[]>`
      SELECT DISTINCT tax_regime FROM fiscal_configurations
       WHERE tenant_id=${user.tenantId} AND tax_regime IS NOT NULL`).map((row) => row.tax_regime);

    const problems = validateProductProfile(body, regimes);
    if (problems.length) throw new ApiError(422, `${product.name}: ${problems.join(" ")}`);

    await tx`
      INSERT INTO fiscal_product_profiles
        (tenant_id, product_id, ncm, cfop, cest, unit_code, origin, csosn, cst_icms,
         cst_pis, cst_cofins, updated_at)
      VALUES (${user.tenantId}, ${body.product_id}, ${body.ncm}, ${body.cfop},
              ${body.cest}, ${body.unit_code}, ${body.origin}, ${body.csosn},
              ${body.cst_icms}, ${body.cst_pis}, ${body.cst_cofins}, now())
      ON CONFLICT (tenant_id, product_id) DO UPDATE SET
        ncm = EXCLUDED.ncm, cfop = EXCLUDED.cfop, cest = EXCLUDED.cest,
        unit_code = EXCLUDED.unit_code, origin = EXCLUDED.origin,
        csosn = EXCLUDED.csosn, cst_icms = EXCLUDED.cst_icms,
        cst_pis = EXCLUDED.cst_pis, cst_cofins = EXCLUDED.cst_cofins, updated_at = now()`;

    // Auditado com os códigos novos: mudar tributação de produto é o tipo de
    // alteração que alguém precisa conseguir datar e atribuir na fiscalização.
    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'fiscal_profile_updated',
              ${tx.json({
                product_id: body.product_id, product: product.name, ncm: body.ncm,
                cfop: body.cfop, csosn: body.csosn, cst_icms: body.cst_icms,
                cst_pis: body.cst_pis, cst_cofins: body.cst_cofins,
              } as never)},
              ${clientIp(request)})`;
    return { product_id: body.product_id, name: product.name };
  });

  return json({ saved });
});
