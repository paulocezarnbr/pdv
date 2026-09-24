/**
 * `GET /api/panel/menu/qr?id=` — o QR do link, em SVG para imprimir.
 *
 * SVG e não PNG: imprime nítido em qualquer tamanho, do adesivo de mesa ao
 * banner da vitrine. Correção de erro "M" (15%): o QR de mesa sofre respingo e
 * desgaste, e o nível mais alto aumentaria a densidade sem ganho real no
 * tamanho de impressão usual.
 */

import { NextResponse } from "next/server";
import QRCode from "qrcode";
import { z } from "zod";

import { requirePanelUser } from "@/lib/auth/panel";
import { withTenant } from "@/lib/db";
import { ApiError, handler } from "@/lib/http";
import { menuUrl } from "@/lib/menu/admin";

export const dynamic = "force-dynamic";

export const GET = handler(async (request) => {
  const user = await requirePanelUser(request);
  const id = new URL(request.url).searchParams.get("id") ?? "";
  if (!z.string().uuid().safeParse(id).success) throw new ApiError(400, "Link inválido.");

  const [link] = await withTenant(user.tenantId, (tx) => tx<{ token: string }[]>`
    SELECT token FROM menu_links
     WHERE id = ${id} AND tenant_id = ${user.tenantId} AND revoked_at IS NULL
  `);
  if (!link) throw new ApiError(404, "Link não encontrado ou revogado.");

  const svg = await QRCode.toString(menuUrl(request, link.token), {
    type: "svg",
    errorCorrectionLevel: "M",
    margin: 2,
    color: { dark: "#111111", light: "#ffffff" },
  });
  return new NextResponse(svg, {
    headers: {
      "content-type": "image/svg+xml; charset=utf-8",
      // Privado: o QR aponta para um link que o dono pode revogar a qualquer
      // momento, e um cache compartilhado continuaria servindo o antigo.
      "cache-control": "private, no-store",
    },
  });
});
