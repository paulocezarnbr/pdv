/**
 * Assinatura dos comandos do painel — **cópia deliberada** do terminal.
 *
 * O original é `pdv/remote/protocol.py`, em Python, no app desktop. Esta é a
 * segunda implementação do mesmo HMAC, agora em TypeScript, e a duplicação é
 * intencional: a nuvem assina, o terminal confere, e o terminal precisa
 * continuar conferindo mesmo quando **esta API é a parte comprometida**.
 * Importar uma biblioteca compartilhada uniria os dois lados num ponto único
 * de falha justamente onde a separação é a proteção.
 *
 * O preço da duplicação é conhecido: duas implementações do mesmo HMAC
 * divergem em algum detalhe de serialização — ordem de chave, espaço, acento
 * escapado — e a divergência aparece como "o painel parou de funcionar" numa
 * sexta-feira à noite, sem nada no log dizendo por quê. Por isso existe um
 * teste de contrato que compara as duas **byte a byte**, rodando o Python e o
 * Node sobre os mesmos payloads (`tests/contract.test.ts` aqui e
 * `test_remote_transport.py` lá). Quem mexer numa quebra o CI, não a loja.
 *
 * Dois detalhes que não podem mudar sem mudar o outro lado
 * --------------------------------------------------------
 *
 * 1. **`ensure_ascii=False`.** O Python do terminal serializa `"ç"` como o
 *    caractere, não como `ç`. O `JSON.stringify` do Node já faz isso, mas
 *    escrever a razão aqui evita que alguém "conserte" para escapar.
 * 2. **Separador `\x1f`.** Unit Separator não aparece em UUID nem em ISO-8601,
 *    então concatenar os campos com ele não permite que um campo malicioso
 *    finja ser dois.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

/** Os únicos comandos que o terminal aceita. Espelha `CommandKind` lá. */
export const COMMAND_KINDS = ["apply_discount", "cancel_item"] as const;
export type CommandKind = (typeof COMMAND_KINDS)[number];

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

/**
 * JSON determinístico: chaves ordenadas, sem espaço supérfluo, acento cru.
 *
 * O `JSON.stringify` do Node não ordena chaves — e a ordem é o que muda o
 * texto que entra no HMAC. Um objeto montado em outra ordem no painel geraria
 * assinatura diferente para o mesmo conteúdo, e o terminal recusaria um
 * comando legítimo.
 *
 * A ordenação é **recursiva**: um payload aninhado com as chaves internas em
 * outra ordem tem o mesmo problema, um nível abaixo, onde ninguém procura.
 */
export function canonicalPayload(payload: unknown): string {
  return JSON.stringify(sortKeys(payload));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    // Array NÃO é ordenado: a ordem dos elementos é conteúdo, não apresentação.
    // Ordenar aqui faria `[3,1,2]` e `[1,2,3]` assinarem igual.
    return value.map(sortKeys);
  }
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) {
      sorted[key] = sortKeys(source[key]);
    }
    return sorted;
  }
  return value;
}

export interface CommandMaterial {
  commandUuid: string;
  deviceId: string;
  kind: string;
  payload: unknown;
  issuedAt: string;
}

/** O texto exato que entra no HMAC. Separado para o teste de contrato vê-lo. */
export function signingMaterial(command: CommandMaterial): string {
  return [
    command.commandUuid,
    command.deviceId,
    command.kind,
    canonicalPayload(command.payload),
    command.issuedAt,
  ].join("\x1f");
}

export function signCommand(secret: Buffer, command: CommandMaterial): string {
  return createHmac("sha256", secret)
    .update(signingMaterial(command), "utf8")
    .digest("hex");
}

/**
 * Comparação em tempo constante.
 *
 * Comparar hash com `===` vaza informação pelo tempo de resposta e permitiria
 * forjar uma assinatura byte a byte, uma tentativa por vez. O custo de fazer
 * certo é uma linha.
 */
export function signaturesMatch(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  // `timingSafeEqual` lança se os tamanhos diferem — o que por si só já
  // vazaria o tamanho. Comparar o tamanho antes é inevitável e inofensivo:
  // o tamanho de um hex de SHA-256 é sempre o mesmo.
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}
