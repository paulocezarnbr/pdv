/** PIN operacional do PDV: compatível com o Argon2id do desktop Python. */

import { hash, type Options } from "@node-rs/argon2";

const FORBIDDEN = new Set([
  "123456", "654321", "112233", "121212", "123123", "000000",
  "111111", "696969", "666666", "159753", "147258", "102030",
  "123321", "010203",
]);

function isRun(pin: string, step: number): boolean {
  return [...pin.slice(0, -1)].every((digit, index) =>
    (Number(pin[index + 1]) - Number(digit) + 10) % 10 === (step + 10) % 10,
  );
}

function isAlternating(pin: string): boolean {
  return [...pin].every((digit, index) => digit === pin[index % 2]);
}

export function validatePin(pin: string): string {
  if (!/^\d{6,12}$/.test(pin)) {
    throw new Error("O PIN deve ter entre 6 e 12 dígitos.");
  }
  if (
    FORBIDDEN.has(pin)
    || new Set(pin).size === 1
    || isRun(pin, 1)
    || isRun(pin, -1)
    || isAlternating(pin)
  ) {
    throw new Error("Escolha um PIN sem sequências, repetições ou padrão alternado.");
  }
  return pin;
}

export async function hashPin(pin: string): Promise<string> {
  // A biblioteca publica `const enum`, incompatível com isolatedModules do
  // Next. Os valores fazem parte do ABI: 2 = Argon2id; 1 = versão 0x13/19.
  const options: Options = {
    algorithm: 2,
    version: 1,
    memoryCost: 65_536,
    timeCost: 3,
    parallelism: 4,
    outputLen: 32,
  };
  return hash(validatePin(pin), options);
}
