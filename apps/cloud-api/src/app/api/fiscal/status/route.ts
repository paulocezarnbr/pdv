import { z } from "zod";

import { requireDevice } from "@/lib/auth/device";
import { fiscalDocumentStatus } from "@/lib/fiscal/service";
import { ApiError, handler, json } from "@/lib/http";

export const dynamic = "force-dynamic";

const Query = z.string().uuid();

export const GET = handler(async (request) => {
  const device = await requireDevice(request);
  const raw = new URL(request.url).searchParams.get("request_uuid");
  const parsed = Query.safeParse(raw);
  if (!parsed.success) throw new ApiError(422, "request_uuid inválido.");
  return json(await fiscalDocumentStatus(device, parsed.data));
});
