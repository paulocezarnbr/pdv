/**
 * O código de ativação: gerado no painel, ditado ao balcão, trocado por token.
 *
 * Mora aqui, e não em cada rota, porque quem gera e quem confere precisam
 * concordar sobre a forma canônica. O PDV descarta tudo que não é letra ou
 * número antes de enviar (`normalize_code` no desktop); se o painel mostrasse
 * `ABCD-EFGH` e guardasse o hash com o hífen, todo código gerado seria recusado
 * como "inválido" — com a mensagem genérica que existe justamente para não
 * dizer por quê.
 */

import { createHash, randomInt } from "node:crypto";

/** Validade do código a partir da geração no painel. */
export const CODE_TTL_MINUTES = 15;

/**
 * Sem 0/O, 1/I/L: o código é lido em voz alta e digitado por outra pessoa.
 * 31 símbolos x 12 posições ≈ 59 bits — com 10 tentativas por IP a cada 15
 * minutos, adivinhar é inviável dentro da validade.
 */
const ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
const LENGTH = 12;

/** A forma que o PDV envia: só letras e números, em maiúsculas. */
export function canonicalCode(raw: string): string {
  return raw.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

/**
 * SHA-256 sem sal basta: o código é aleatório de alta entropia e vive 15
 * minutos, então não há dicionário a proteger — diferente de uma senha
 * escolhida por gente. O banco guarda só isto; um dump não entrega código ativo.
 */
export function codeHash(raw: string): string {
  return createHash("sha256").update(canonicalCode(raw), "utf8").digest("hex");
}

/** Um código novo, em grupos de quatro para ditar: `ABCD-EFGH-JKMN`. */
export function newActivationCode(): string {
  let code = "";
  for (let i = 0; i < LENGTH; i++) code += ALPHABET[randomInt(ALPHABET.length)];
  return code.match(/.{4}/g)!.join("-");
}
