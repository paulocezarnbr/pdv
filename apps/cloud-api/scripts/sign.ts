/**
 * Assina um comando e imprime o resultado. É a ponte do teste de contrato.
 *
 * A nuvem virou TypeScript; o terminal continua em Python. As duas
 * implementações do mesmo HMAC precisam concordar byte a byte, e a única forma
 * de comparar de verdade é executar as duas — não reimplementar uma na outra
 * linguagem dentro do teste, que é comparar uma cópia com outra cópia.
 *
 * O teste do desktop (`test_remote_transport.py`) chama este script com os
 * payloads que costumam divergir — ordem de chave, acento, tipo numérico,
 * aninhamento — e compara a saída com a do `pdv/remote/protocol.py`.
 *
 * Uso:
 *     node --experimental-strip-types scripts/sign.ts '<json>'
 *
 * Entrada (JSON, por argumento ou stdin):
 *     {"secret_hex": "...", "command_uuid": "...", "device_id": "...",
 *      "kind": "...", "payload": {...}, "issued_at": "..."}
 *
 * Saída (JSON):
 *     {"signature": "...", "canonical_payload": "...", "kinds": [...]}
 */

import { COMMAND_KINDS, canonicalPayload, signCommand } from "../src/lib/crypto/commands.ts";

interface Input {
  secret_hex: string;
  command_uuid: string;
  device_id: string;
  kind: string;
  payload: unknown;
  issued_at: string;
}

async function readInput(): Promise<Input> {
  const arg = process.argv[2];
  if (arg) return JSON.parse(arg) as Input;

  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Input;
}

const input = await readInput();

process.stdout.write(
  JSON.stringify({
    signature: signCommand(Buffer.from(input.secret_hex, "hex"), {
      commandUuid: input.command_uuid,
      deviceId: input.device_id,
      kind: input.kind,
      payload: input.payload,
      issuedAt: input.issued_at,
    }),
    canonical_payload: canonicalPayload(input.payload),
    kinds: [...COMMAND_KINDS],
  }),
);
