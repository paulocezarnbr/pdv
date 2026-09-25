/**
 * O IP que alimenta o limite de tentativas: o que o cliente consegue forjar e
 * o que não consegue, em cada caminho até o app.
 */

import { afterEach, describe, expect, it } from "vitest";

import { clientIp, parseIp } from "../src/lib/client-ip.ts";

function ip(headers: Record<string, string>): string {
  return clientIp(new Request("http://localhost/api/devices/activate", { headers }));
}

/** Uma borda da Cloudflare (162.158.0.0/15) e um endereço do Traefik no Docker. */
const EDGE = "162.158.12.34";
const TRAEFIK = "10.0.1.5";
const CLIENT = "198.51.100.20";

describe("clientIp", () => {
  afterEach(() => {
    delete process.env.TRUSTED_PROXY_CIDRS;
  });

  it("XFF forjado pela Cloudflare: vale o CF-Connecting-IP", () => {
    // O cliente mandou "1.2.3.4"; a Cloudflare acrescentou o IP real e o
    // Traefik, a borda que falou com ele.
    expect(ip({
      "x-forwarded-for": `1.2.3.4, ${CLIENT}, ${EDGE}`,
      "cf-connecting-ip": CLIENT,
      "x-real-ip": EDGE,
    })).toBe(CLIENT);
  });

  it("trocar o XFF a cada tentativa não troca o IP", () => {
    const seen = new Set(
      ["1.1.1.1", "2.2.2.2", "3.3.3.3"].map((forged) =>
        ip({ "x-forwarded-for": `${forged}, ${CLIENT}, ${EDGE}`, "cf-connecting-ip": CLIENT })),
    );
    expect([...seen]).toEqual([CLIENT]);
  });

  it("direto na VPS, sem passar pela Cloudflare, o CF-Connecting-IP forjado é ignorado", () => {
    // O Traefik anotou quem de fato conectou; não é uma borda da Cloudflare.
    expect(ip({
      "x-forwarded-for": CLIENT,
      "cf-connecting-ip": "203.0.113.99",
      "x-real-ip": CLIENT,
    })).toBe(CLIENT);
  });

  it("só XFF: o primeiro da direita que não é proxy", () => {
    expect(ip({ "x-forwarded-for": `1.2.3.4, ${CLIENT}, ${EDGE}, ${TRAEFIK}` })).toBe(CLIENT);
    expect(ip({ "x-forwarded-for": `1.2.3.4, ${CLIENT}` })).toBe(CLIENT);
    expect(ip({ "x-forwarded-for": CLIENT })).toBe(CLIENT);
  });

  it("só XFF com todos os saltos internos: o mais antigo registrado", () => {
    expect(ip({ "x-forwarded-for": `172.18.0.3, ${TRAEFIK}` })).toBe("172.18.0.3");
  });

  it("TRUSTED_PROXY_CIDRS acrescenta um proxy próprio", () => {
    const headers = { "x-forwarded-for": `${CLIENT}, 203.0.113.7` };
    expect(ip(headers)).toBe("203.0.113.7");
    process.env.TRUSTED_PROXY_CIDRS = "203.0.113.0/24, bobagem/99";
    expect(ip(headers)).toBe(CLIENT);
  });

  it("só X-Real-Ip", () => {
    expect(ip({ "x-real-ip": ` ${CLIENT} ` })).toBe(CLIENT);
  });

  it("nada: desconhecido", () => {
    expect(ip({})).toBe("desconhecido");
    expect(ip({ "x-forwarded-for": " , ", "x-real-ip": "" })).toBe("desconhecido");
  });

  it("IPv6, porta e IPv4 mapeado são normalizados", () => {
    expect(ip({ "x-forwarded-for": `[2001:DB8::1]:443, ${TRAEFIK}` })).toBe("2001:db8::1");
    expect(ip({ "x-forwarded-for": "198.51.100.20:51234" })).toBe(CLIENT);
    expect(ip({ "x-forwarded-for": `::ffff:${CLIENT}` })).toBe(CLIENT);
    expect(ip({
      "x-forwarded-for": `2001:db8::7, 2606:4700::1`,
      "cf-connecting-ip": "2001:db8::7",
    })).toBe("2001:db8::7");
  });

  it("sem nenhum proxy à vista, o CF-Connecting-IP é o que há", () => {
    expect(ip({ "cf-connecting-ip": CLIENT })).toBe(CLIENT);
  });

  it("parseIp recusa o que não é endereço", () => {
    expect(parseIp("ativacao-123")).toBeNull();
    expect(parseIp("999.1.1.1")).toBeNull();
    expect(parseIp("")).toBeNull();
  });
});
