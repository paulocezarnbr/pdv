/**
 * A cadeia de auditoria — recalculada aqui, nunca aceita de palavra.
 *
 * O terminal afirma integridade; o servidor confere. Nunca se aceita o
 * autoatestado de um banco que fica na máquina do caixa, porque quem tem a
 * máquina tem o banco — e a única garantia forte do sistema é esta: **o evento
 * já sincronizado não pode ser apagado da nuvem**.
 *
 * Espelha `services/audit.py` no desktop, e vale aqui o mesmo aviso de
 * `crypto/commands.ts`: divergir invalidaria toda cadeia legítima de uma vez,
 * em todas as lojas, e o sintoma seria "o sistema acusa fraude em todo mundo".
 */

import { createHmac, timingSafeEqual } from "node:crypto";

export interface ChainLink {
  prevHash: string;
  seq: number;
  eventType: string;
  payloadJson: string;
  createdAt: string;
}

/** Idêntico ao cliente. O separador `|` acompanha o formato original. */
export function computeChainHash(secret: Buffer, link: ChainLink): string {
  const material = [
    link.prevHash,
    String(link.seq),
    link.eventType,
    link.payloadJson,
    link.createdAt,
  ].join("|");
  return createHmac("sha256", secret).update(material, "utf8").digest("hex");
}

export function hashesMatch(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}
