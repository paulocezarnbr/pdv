/**
 * O que a pessoa digita no painel, convertido sem surpresa.
 *
 * No Brasil se digita "12,5". `Number("12,5")` é `NaN`, e `parseFloat("12,5")`
 * é 12 — a contagem de 12,5 kg viraria 12 kg calada, e a sugestão de compra
 * sairia meio quilo maior. Aqui vírgula e ponto valem igual, e o resto é recusa.
 */

/** "12,5" ou "12.5" — no balcão se digita com vírgula. `null` se não for número. */
export function parseKg(text: string): number | null {
  const normalized = text.trim().replace(/\s/g, "").replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(normalized)) return null;
  const value = Number(normalized);
  return Number.isFinite(value) && value <= 100_000 ? value : null;
}

