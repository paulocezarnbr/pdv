/**
 * O IP de quem fez a requisição, para os limitadores de tentativa e a auditoria.
 *
 * O caminho em produção
 * ---------------------
 *
 *     navegador/PDV → Cloudflare (proxy do DNS) → Traefik do Coolify → app
 *
 * O `X-Forwarded-For` chega com o que o cliente mandou **mais** o que cada
 * proxy acrescentou à direita. O primeiro item é, portanto, o que o cliente
 * quis escrever: usá-lo — como este código fazia — deixava qualquer um trocar
 * de "IP" a cada tentativa e passar pelo limite de 10 ativações por 15
 * minutos, a única barreira de força bruta do único endpoint sem token.
 *
 * A regra
 * -------
 *
 * 1. **`CF-Connecting-IP`, se a requisição veio da Cloudflare.** A Cloudflare
 *    sobrescreve esse cabeçalho em toda requisição. Mas ele só vale se o salto
 *    mais próximo (o último item do `X-Forwarded-For`, ou o `X-Real-Ip` do
 *    Traefik) for uma borda da Cloudflare: quem acha o IP do servidor e fala
 *    direto com o Traefik também consegue escrever `CF-Connecting-IP`.
 * 2. **Senão, o `X-Forwarded-For` lido da direita para a esquerda**, pulando os
 *    proxies confiáveis (rede interna do Docker, bordas da Cloudflare e
 *    `TRUSTED_PROXY_CIDRS`). O primeiro que não é proxy é o cliente: dali para
 *    a esquerda, qualquer coisa pode ter sido forjada.
 * 3. **Senão, `X-Real-Ip`**, e por fim `"desconhecido"`.
 *
 * A premissa que sustenta tudo: o app **nunca** é exposto sem o Traefik na
 * frente. Exposto direto, todo cabeçalho aqui é do cliente. E o ideal é que a
 * porta 443 da VPS só aceite as faixas da Cloudflare (firewall), o que tira o
 * desvio do passo 1 da mesa de vez.
 */

import { BlockList, isIP } from "node:net";

/**
 * Bordas da Cloudflare (https://www.cloudflare.com/ips/). Mudam raramente; se a
 * Cloudflare anunciar faixa nova, acrescente aqui ou em `TRUSTED_PROXY_CIDRS`.
 */
const CLOUDFLARE_RANGES = [
  "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
  "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
  "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
  "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
  "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
  "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
];

/** Onde moram o Traefik e a rede do Docker: nunca é o cliente final. */
const PRIVATE_RANGES = [
  "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
  "::1/128", "fc00::/7", "fe80::/10",
];

function blockList(ranges: string[]): BlockList {
  const list = new BlockList();
  for (const range of ranges) {
    const [address, bits] = range.trim().split("/");
    const family = isIP(address ?? "");
    const prefix = Number(bits);
    if (!family || !Number.isInteger(prefix) || prefix < 0 || prefix > (family === 4 ? 32 : 128)) {
      // Faixa mal escrita no ambiente: o proxy dela deixa de ser pulado, e os
      // clientes passam a dividir o IP dele no limitador. Avisar alto.
      console.error(`[client-ip] faixa inválida ignorada: ${JSON.stringify(range)}`);
      continue;
    }
    list.addSubnet(address!, prefix, family === 4 ? "ipv4" : "ipv6");
  }
  return list;
}

const cloudflare = blockList(CLOUDFLARE_RANGES);
let trusted: { env: string; list: BlockList } | null = null;

function trustedProxies(): BlockList {
  const env = process.env.TRUSTED_PROXY_CIDRS ?? "";
  if (trusted?.env !== env) {
    const extra = env.split(",").map((range) => range.trim()).filter(Boolean);
    trusted = { env, list: blockList([...PRIVATE_RANGES, ...CLOUDFLARE_RANGES, ...extra]) };
  }
  return trusted.list;
}

/**
 * O endereço de um item de cabeçalho, sem porta nem colchetes, ou `null` se
 * não for IP. IPv4 dentro de IPv6 (`::ffff:1.2.3.4`) vira IPv4.
 */
export function parseIp(raw: string | null | undefined): string | null {
  let value = raw?.trim() ?? "";
  if (!value) return null;
  const bracketed = /^\[([^\]]+)\](?::\d+)?$/.exec(value);
  if (bracketed) value = bracketed[1]!;
  else if (/^\d{1,3}(?:\.\d{1,3}){3}:\d+$/.test(value)) value = value.slice(0, value.lastIndexOf(":"));
  const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(value);
  if (mapped) value = mapped[1]!;
  return isIP(value) ? value.toLowerCase() : null;
}

function inList(list: BlockList, ip: string): boolean {
  return list.check(ip, isIP(ip) === 4 ? "ipv4" : "ipv6");
}

/** O IP de origem da requisição. Ver a regra no topo do arquivo. */
export function clientIp(request: Request): string {
  const headers = request.headers;
  const forwarded = (headers.get("x-forwarded-for") ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const realIp = headers.get("x-real-ip")?.trim() || null;

  // 1. Cloudflare — só se quem entregou a requisição ao Traefik foi ela.
  const connecting = parseIp(headers.get("cf-connecting-ip"));
  if (connecting) {
    const nearest = parseIp(forwarded.at(-1) ?? realIp);
    const nearestUnknown = forwarded.length === 0 && realIp === null;
    if (nearestUnknown || (nearest !== null && inList(cloudflare, nearest))) return connecting;
  }

  // 2. X-Forwarded-For da direita para a esquerda, pulando proxies confiáveis.
  const proxies = trustedProxies();
  for (let index = forwarded.length - 1; index >= 0; index--) {
    const entry = forwarded[index]!;
    const ip = parseIp(entry);
    if (ip !== null && inList(proxies, ip)) continue;
    // Item que não é IP só aparece onde nenhum proxy acrescentou nada — e aí
    // tudo é do cliente de qualquer forma. Vale como chave do limitador.
    return ip ?? entry;
  }
  // Só proxies no caminho (rede interna): o salto mais antigo registrado.
  if (forwarded.length > 0) return parseIp(forwarded[0]) ?? forwarded[0]!;

  // 3. O que o Traefik viu, e o fim da linha.
  return (realIp && (parseIp(realIp) ?? realIp)) || "desconhecido";
}
