import { z } from "zod";

import { requireDevice } from "@/lib/auth/device";
import { issueFiscalDocument } from "@/lib/fiscal/service";
import { handler, json, parseBody } from "@/lib/http";

export const dynamic = "force-dynamic";

const Schema = z.object({
  request_uuid: z.string().uuid(),
  order_id: z.string().uuid(),
});

export const POST = handler(async (request) => {
  const device = await requireDevice(request);
  const body = await parseBody(request, Schema);
  const result = await issueFiscalDocument(device, body.request_uuid, body.order_id);
  const status = result.document.status === "unknown" ? 202 : 200;
  return json(result.document, { status });
});
