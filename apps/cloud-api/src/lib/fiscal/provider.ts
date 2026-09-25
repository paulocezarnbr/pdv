import { z } from "zod";

import { env } from "@/lib/env";

export type FiscalProviderStatus = "authorized" | "rejected" | "unknown";

export interface FiscalItemIntent {
  productId: string;
  name: string;
  quantity: string;
  unitPriceCents: number;
  totalCents: number;
  ncm: string;
  cfop: string;
  cest?: string;
  unitCode: string;
  origin: number;
  csosn?: string;
  cstIcms?: string;
  cstPis: string;
  cstCofins: string;
}

export interface FiscalIntent {
  documentId: string;
  requestUuid: string;
  orderId: string;
  tenantId: string;
  storeId: string;
  deviceId: string;
  model: number;
  series: number;
  number: number;
  environment: "homologation" | "production";
  certificateRef: string;
  /** Só com o QR Code v2; o v3, padrão do emissor, dispensa o CSC. */
  cscRef?: string;
  cscId?: string;
  issuer: {
    uf: string;
    cnpj: string;
    stateRegistration: string;
    taxRegime: number;
    legalName: string;
    address: Record<string, unknown>;
  };
  totalCents: number;
  items: FiscalItemIntent[];
  /** A NFC-e exige o grupo de pagamento; o troco vai em `changeCents`. */
  payments: FiscalPaymentIntent[];
}

export interface FiscalPaymentIntent {
  method: string;
  amountCents: number;
  changeCents: number;
}

export interface FiscalProviderResult {
  status: FiscalProviderStatus;
  code: string;
  reason: string;
  accessKey?: string;
  protocol?: string;
  processedXml?: string;
}

export interface FiscalProvider {
  authorize(intent: FiscalIntent): Promise<FiscalProviderResult>;
  query(requestUuid: string): Promise<FiscalProviderResult>;
}

const ResultSchema = z.object({
  status: z.enum(["authorized", "rejected", "unknown"]),
  code: z.string().max(32).default(""),
  reason: z.string().max(1000),
  access_key: z.string().max(64).optional(),
  protocol: z.string().max(64).optional(),
  processed_xml: z.string().max(2_000_000).optional(),
});

export class FiscalProviderUnavailable extends Error {
  constructor(message = "Serviço fiscal indisponível; o resultado é desconhecido.") {
    super(message);
    this.name = "FiscalProviderUnavailable";
  }
}

/**
 * O serviço fiscal interno (`apps/fiscal-net`, C# com DFe.NET) pela rede do
 * Coolify. O contrato HTTP é o mesmo do serviço em Python que ele substituiu.
 */
export class HttpFiscalProvider implements FiscalProvider {
  private readonly baseUrl: string;
  private readonly token: string;

  constructor(options?: { baseUrl?: string; token?: string }) {
    this.baseUrl = (options?.baseUrl ?? env.fiscalServiceUrl ?? "").replace(/\/$/, "");
    this.token = options?.token ?? env.fiscalServiceToken ?? "";
    if (!this.baseUrl || !this.token) {
      throw new FiscalProviderUnavailable("Serviço fiscal interno não configurado.");
    }
  }

  authorize(intent: FiscalIntent): Promise<FiscalProviderResult> {
    return this.call("/v1/fiscal/authorize", intent);
  }

  query(requestUuid: string): Promise<FiscalProviderResult> {
    return this.call("/v1/fiscal/status", { request_uuid: requestUuid });
  }

  private async call(path: string, body: unknown): Promise<FiscalProviderResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), env.fiscalTimeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${this.token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) {
        // Não devolve o corpo: ele pode conter detalhe da biblioteca ou segredo.
        throw new FiscalProviderUnavailable(`Serviço fiscal respondeu HTTP ${response.status}.`);
      }
      const parsed = ResultSchema.safeParse(await response.json());
      if (!parsed.success) throw new FiscalProviderUnavailable("Resposta fiscal inválida.");
      return {
        status: parsed.data.status,
        code: parsed.data.code,
        reason: parsed.data.reason,
        accessKey: parsed.data.access_key,
        protocol: parsed.data.protocol,
        processedXml: parsed.data.processed_xml,
      };
    } catch (error) {
      if (error instanceof FiscalProviderUnavailable) throw error;
      throw new FiscalProviderUnavailable();
    } finally {
      clearTimeout(timer);
    }
  }
}
