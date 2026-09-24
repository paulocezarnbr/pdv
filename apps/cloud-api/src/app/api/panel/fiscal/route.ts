/**
 * `GET/PUT /api/panel/fiscal` — o cadastro fiscal da loja, pelo dono.
 *
 * Só o proprietário. O cadastro fiscal decide o CNPJ, o regime e o certificado
 * em nome dos quais as notas saem; um gerente que o alterasse passaria a emitir
 * em nome de outra inscrição, e a responsabilidade tributária continuaria sendo
 * do dono.
 *
 * O certificado A1 e o CSC **não passam por aqui**. O cadastro guarda o NOME da
 * referência no cofre do serviço fiscal (ver `lib/fiscal/registry.ts`); um
 * corpo com campo que pareça senha ou arquivo é recusado com a explicação.
 */

import { z } from "zod";

import { requirePanelUser, type PanelUser } from "@/lib/auth/panel";
import { withTenant, type Tx } from "@/lib/db";
import { env } from "@/lib/env";
import {
  digits,
  secretLikeFields,
  validateFiscalConfig,
  validateProductProfile,
} from "@/lib/fiscal/registry";
import { ApiError, clientIp, handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

const ConfigSchema = z
  .object({
    store_id: z.string().uuid(),
    uf: z.string().length(2).transform((value) => value.toUpperCase()),
    environment: z.enum(["homologation", "production"]),
    cnpj: z.string().max(20),
    state_registration: z.string().max(20),
    tax_regime: z.number().int(),
    legal_name: z.string().max(60),
    address: z
      .object({
        street: z.string().max(60),
        number: z.string().max(60),
        district: z.string().max(60),
        city_code: z.string().max(7),
        city: z.string().max(60),
        zip: z.string().max(9),
      })
      .strict(),
    certificate_ref: z.string().max(120).default(""),
    csc_ref: z.string().max(120).default(""),
    csc_id: z.string().max(6).default(""),
    enabled: z.boolean(),
    normal_series: z.number().int().min(1).max(999).optional(),
  })
  // `strict`: campo desconhecido é recusado, não ignorado. Ignorar em
  // silêncio um `certificate_password` daria a impressão de que ele foi
  // guardado — e ele teria passado pelo log do proxy do mesmo jeito.
  .strict();

function requireOwner(user: PanelUser): void {
  if (user.role !== "owner") {
    throw new ApiError(403, "Somente o proprietário altera o cadastro fiscal.");
  }
}

async function requireStore(tx: Tx, tenantId: string, storeId: string): Promise<void> {
  const rows = await tx`SELECT 1 FROM stores WHERE id=${storeId} AND tenant_id=${tenantId}`;
  // 404 e não 403: a loja não existe *neste tenant*.
  if (!rows[0]) throw new ApiError(404, "Loja não encontrada.");
}

// --------------------------------------------------------------------------- //
// Leitura: configuração, série e o que ainda impede emitir
// --------------------------------------------------------------------------- //

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  requireOwner(user);
  const storeId = new URL(request.url).searchParams.get("store") ?? "";
  if (!z.string().uuid().safeParse(storeId).success) throw new ApiError(422, "Loja inválida.");

  const result = await withTenant(user.tenantId, async (tx) => {
    await requireStore(tx, user.tenantId, storeId);

    const [config] = await tx<Record<string, unknown>[]>`
      SELECT uf, environment, cnpj, state_registration, tax_regime, legal_name,
             address_json, certificate_ref, csc_ref, csc_id, enabled, updated_at
        FROM fiscal_configurations
       WHERE tenant_id=${user.tenantId} AND store_id=${storeId}`;
    const [series] = await tx<{ series: number; next_number: string }[]>`
      SELECT series, next_number FROM fiscal_series
       WHERE tenant_id=${user.tenantId} AND store_id=${storeId}
         AND model=65 AND purpose='normal' AND device_id IS NULL`;
    const regimes = (await tx<{ tax_regime: number }[]>`
      SELECT DISTINCT tax_regime FROM fiscal_configurations
       WHERE tenant_id=${user.tenantId} AND tax_regime IS NOT NULL`).map((row) => row.tax_regime);

    const products = await tx<{
      id: string; sku: string; name: string; price_cents: string;
      ncm: string | null; cfop: string | null; cest: string | null; unit_code: string | null;
      origin: number | null; csosn: string | null; cst_icms: string | null;
      cst_pis: string | null; cst_cofins: string | null;
    }[]>`
      SELECT p.id::text AS id, p.sku, p.name, p.price_cents::text AS price_cents,
             fp.ncm, fp.cfop, fp.cest, fp.unit_code, fp.origin,
             fp.csosn, fp.cst_icms, fp.cst_pis, fp.cst_cofins
        FROM products p
        LEFT JOIN fiscal_product_profiles fp
          ON fp.tenant_id = p.tenant_id AND fp.product_id = p.id::text
       WHERE p.tenant_id=${user.tenantId} AND p.is_active
       ORDER BY p.name`;

    const withStatus = products.map((product) => {
      const problems = product.ncm
        ? validateProductProfile({
            ncm: product.ncm, cfop: product.cfop ?? "", cest: product.cest,
            unit_code: product.unit_code ?? "", origin: product.origin ?? -1,
            csosn: product.csosn, cst_icms: product.cst_icms,
            cst_pis: product.cst_pis ?? "", cst_cofins: product.cst_cofins ?? "",
          }, regimes)
        : ["Sem perfil tributário."];
      return { ...product, complete: problems.length === 0, problems };
    });

    // O que impede a primeira nota, em linguagem de quem vai resolver. É a
    // lista que o dono lê antes de ligar a emissão — e é mais útil que um
    // botão desabilitado sem explicação.
    const blockers: string[] = [];
    if (!config) blockers.push("Cadastro fiscal da loja não preenchido.");
    else if (!config.enabled) blockers.push("Emissão desligada nesta loja.");
    if (!series) blockers.push("Série normal da NFC-e não definida.");
    const incomplete = withStatus.filter((product) => !product.complete).length;
    if (incomplete) blockers.push(`${incomplete} produto(s) sem perfil tributário completo.`);
    if (!env.fiscalConfigured) blockers.push("Serviço fiscal não configurado na retaguarda.");

    return {
      config: config ?? null,
      series: series ? { series: series.series, next_number: Number(series.next_number) } : null,
      products: withStatus,
      blockers,
      production_enabled: env.fiscalProductionEnabled,
    };
  });

  return json(result);
});

// --------------------------------------------------------------------------- //
// Gravação da configuração da loja
// --------------------------------------------------------------------------- //

export const PUT = handler(async (request) => {
  const user = await requirePanelUser(request);
  requireOwner(user);

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    throw new ApiError(400, "Corpo da requisição não é JSON válido.");
  }
  const leaked = secretLikeFields(raw);
  if (leaked.length) {
    throw new ApiError(
      422,
      "O certificado A1, sua senha e o CSC nunca passam por esta tela. Envie-os " +
        "ao cofre do serviço fiscal e informe aqui só o nome da referência. " +
        `Campo recusado: ${leaked.join(", ")}.`,
    );
  }
  const parsed = ConfigSchema.safeParse(raw);
  if (!parsed.success) {
    throw new ApiError(422, parsed.error.issues
      .map((issue) => `${issue.path.join(".") || "corpo"}: ${issue.message}`).join("; "));
  }
  const body = parsed.data;

  const input = {
    ...body,
    cnpj: digits(body.cnpj),
    state_registration: body.state_registration.replace(/[.\-/\s]/g, ""),
    address: { ...body.address, zip: digits(body.address.zip) },
  };

  // Emissão desligada: salva o que já se sabe. É comum ter o CNPJ hoje e o
  // certificado semana que vem, e obrigar tudo de uma vez faria o dono não
  // salvar nada. Para LIGAR, tudo precisa estar completo e coerente.
  const problems = validateFiscalConfig(input, {
    productionEnabled: env.fiscalProductionEnabled,
    requireCredentials: body.enabled,
  });
  if (problems.length) throw new ApiError(422, problems.join(" "));

  const saved = await withTenant(user.tenantId, async (tx) => {
    await requireStore(tx, user.tenantId, body.store_id);

    await tx`
      INSERT INTO fiscal_configurations
        (tenant_id, store_id, uf, environment, cnpj, state_registration, tax_regime,
         legal_name, address_json, certificate_ref, csc_ref, csc_id, enabled, updated_at)
      VALUES (${user.tenantId}, ${body.store_id}, ${input.uf}, ${input.environment},
              ${input.cnpj}, ${input.state_registration}, ${input.tax_regime},
              ${input.legal_name.trim()}, ${tx.json(input.address as never)},
              ${input.certificate_ref || null}, ${input.csc_ref || null},
              ${input.csc_id || null}, ${input.enabled}, now())
      ON CONFLICT (tenant_id, store_id) DO UPDATE SET
        uf = EXCLUDED.uf, environment = EXCLUDED.environment, cnpj = EXCLUDED.cnpj,
        state_registration = EXCLUDED.state_registration,
        tax_regime = EXCLUDED.tax_regime, legal_name = EXCLUDED.legal_name,
        address_json = EXCLUDED.address_json,
        certificate_ref = EXCLUDED.certificate_ref, csc_ref = EXCLUDED.csc_ref,
        csc_id = EXCLUDED.csc_id, enabled = EXCLUDED.enabled, updated_at = now()`;

    if (body.normal_series !== undefined) {
      await defineNormalSeries(tx, user.tenantId, body.store_id, body.normal_series);
    }

    await tx`
      INSERT INTO panel_admin_events (tenant_id, actor_user_id, event_type, payload_json, ip)
      VALUES (${user.tenantId}, ${user.id}, 'fiscal_config_updated',
              ${tx.json({
                store_id: body.store_id, environment: input.environment,
                enabled: input.enabled, cnpj: input.cnpj, tax_regime: input.tax_regime,
                normal_series: body.normal_series ?? null,
                // Só os NOMES das referências — nunca houve segredo neste corpo.
                certificate_ref: input.certificate_ref, csc_ref: input.csc_ref,
              } as never)},
              ${clientIp(request)})`;
    return { store_id: body.store_id, enabled: input.enabled, environment: input.environment };
  });

  return json({ saved });
});

/**
 * Define a série normal da loja. Trocar uma série que já emitiu é recusado.
 *
 * Mesma regra do terminal (`FiscalService.configure_series`): mudar a série no
 * meio da vida da loja deixaria documentos autorizados numa série "órfã", e a
 * numeração recomeçaria em 1 numa série nova — dois caminhos para confundir a
 * escrituração fiscal do mês.
 */
async function defineNormalSeries(
  tx: Tx, tenantId: string, storeId: string, series: number,
): Promise<void> {
  const [current] = await tx<{ id: string; series: number }[]>`
    SELECT id, series FROM fiscal_series
     WHERE tenant_id=${tenantId} AND store_id=${storeId}
       AND model=65 AND purpose='normal' AND device_id IS NULL
     FOR UPDATE`;
  if (!current) {
    await tx`INSERT INTO fiscal_series (tenant_id, store_id, model, series, purpose)
             VALUES (${tenantId}, ${storeId}, 65, ${series}, 'normal')`;
    return;
  }
  if (current.series === series) return;
  const [used] = await tx`
    SELECT 1 FROM fiscal_documents
     WHERE tenant_id=${tenantId} AND store_id=${storeId}
       AND model=65 AND series=${current.series} LIMIT 1`;
  if (used) {
    throw new ApiError(409, `A série ${current.series} já emitiu documentos e não pode ser trocada.`);
  }
  await tx`UPDATE fiscal_series SET series=${series}, updated_at=now() WHERE id=${current.id}`;
}
