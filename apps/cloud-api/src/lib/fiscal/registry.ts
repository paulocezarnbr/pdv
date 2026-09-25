/**
 * Validação do cadastro fiscal — o que o dono digita no painel.
 *
 * Por que validar aqui, e não deixar a SEFAZ recusar
 * --------------------------------------------------
 *
 * A SEFAZ recusa, sim — mas recusa **na hora da venda**, com o cliente no
 * balcão, e depois de a nuvem já ter consumido um número da série. Um NCM de
 * sete dígitos digitado em março vira, em abril, uma fila de notas rejeitadas e
 * de números que precisam de inutilização. O erro de cadastro é barato de
 * pegar no cadastro e caro de pegar em qualquer outro lugar.
 *
 * O que estas regras NÃO fazem
 * ----------------------------
 *
 * Não decidem tributação. Não sugerem CST, não completam NCM, não inferem
 * CSOSN pelo tipo de produto. Isso é trabalho do contador, e um sistema que
 * "adivinha" tributação produz notas autorizadas e erradas — que é pior que
 * notas rejeitadas, porque ninguém percebe. Aqui só se confere **forma** e
 * **coerência interna**: dígitos certos, códigos que existem, regime casado com
 * o tipo de CST.
 */

/** Códigos IBGE das UFs. O código do município começa com o da UF. */
export const UF_CODES: Readonly<Record<string, string>> = {
  RO: "11", AC: "12", AM: "13", RR: "14", PA: "15", AP: "16", TO: "17",
  MA: "21", PI: "22", CE: "23", RN: "24", PB: "25", PE: "26", AL: "27",
  SE: "28", BA: "29", MG: "31", ES: "32", RJ: "33", SP: "35", PR: "41",
  SC: "42", RS: "43", MS: "50", MT: "51", GO: "52", DF: "53",
};

/**
 * Regime tributário (CRT) do emitente.
 * 1 Simples Nacional · 2 Simples com excesso de sublimite · 3 Regime Normal ·
 * 4 MEI.
 */
export const TAX_REGIMES = [1, 2, 3, 4] as const;

/** CRT que tributa ICMS por CSOSN (Simples e MEI); os demais usam CST. */
export function usesCsosn(taxRegime: number): boolean {
  return taxRegime === 1 || taxRegime === 4;
}

/** CSOSN válidos (Simples Nacional). */
export const CSOSN_CODES = new Set([
  "101", "102", "103", "201", "202", "203", "300", "400", "500", "900",
]);

/** CST de ICMS válidos, incluindo os de tributação monofásica (02, 15, 53, 61). */
export const CST_ICMS_CODES = new Set([
  "00", "02", "10", "15", "20", "30", "40", "41", "50", "51", "53", "60",
  "61", "70", "90",
]);

/** CST de PIS e COFINS válidos. */
export const CST_PIS_COFINS_CODES = new Set([
  "01", "02", "03", "04", "05", "06", "07", "08", "09",
  "49", "50", "51", "52", "53", "54", "55", "56",
  "60", "61", "62", "63", "64", "65", "66", "67",
  "70", "71", "72", "73", "74", "75", "98", "99",
]);

/**
 * O que o emissor fiscal (apps/fiscal-net) calcula hoje: tributação sem
 * alíquota nem crédito a destacar. O resto — CSOSN 101 e 201+, CST 00/10/20,
 * PIS/COFINS 01/02 — precisa de percentuais que o cadastro ainda não guarda, e
 * é recusado ANTES de reservar o número, com o nome do produto.
 */
export const ENGINE_CSOSN = new Set(["102", "103", "300", "400", "500"]);
export const ENGINE_CST_ICMS = new Set(["40", "41", "50", "60"]);
export const ENGINE_CST_PIS_COFINS = new Set(["04", "05", "06", "07", "08", "09", "49", "99"]);

/** O motivo de o emissor não conseguir emitir este perfil, ou `null`. */
export function engineGap(profile: {
  csosn?: string | null; cst_icms?: string | null; cst_pis?: string | null; cst_cofins?: string | null;
}): string | null {
  if (profile.csosn && !ENGINE_CSOSN.has(profile.csosn)) return `CSOSN ${profile.csosn}`;
  if (profile.cst_icms && !ENGINE_CST_ICMS.has(profile.cst_icms)) return `CST de ICMS ${profile.cst_icms}`;
  if (profile.cst_pis && !ENGINE_CST_PIS_COFINS.has(profile.cst_pis)) return `CST de PIS ${profile.cst_pis}`;
  if (profile.cst_cofins && !ENGINE_CST_PIS_COFINS.has(profile.cst_cofins)) return `CST de COFINS ${profile.cst_cofins}`;
  return null;
}

/** Os valores de `payments.method` que viram `tPag` na NFC-e. */
export const FISCAL_PAYMENT_METHODS = new Set([
  "cash", "debit", "credit", "pix", "prepaid", "credit_account", "cashback",
]);

export const digits = (value: string): string => value.replace(/\D/g, "");

/**
 * CNPJ com dígitos verificadores conferidos.
 *
 * Confere de verdade, e não só o tamanho: um CNPJ com um dígito trocado tem 14
 * dígitos e é recusado pela SEFAZ em toda nota — e a chave de acesso, que
 * carrega o CNPJ dentro dela, nasceria errada desde a primeira venda.
 */
export function isValidCnpj(value: string): boolean {
  const cnpj = digits(value);
  if (cnpj.length !== 14 || /^(\d)\1{13}$/.test(cnpj)) return false;
  const dv = (base: string): number => {
    let weight = base.length - 7;
    let sum = 0;
    for (const char of base) {
      sum += Number(char) * weight;
      weight = weight === 2 ? 9 : weight - 1;
    }
    const remainder = sum % 11;
    return remainder < 2 ? 0 : 11 - remainder;
  };
  const first = dv(cnpj.slice(0, 12));
  const second = dv(cnpj.slice(0, 12) + first);
  return cnpj.endsWith(`${first}${second}`);
}

/**
 * Referência a um segredo no cofre do serviço fiscal — nunca o segredo.
 *
 * Espelha as regras de `SecretResolver` no serviço fiscal: nome relativo, sem
 * `..`, sem caminho absoluto. Recusar aqui, e não só lá, dá ao dono a mensagem
 * na hora do cadastro, e não na primeira venda.
 */
export function isValidSecretRef(value: string): boolean {
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$/.test(value)) return false;
  return !value.split("/").some((segment) => segment === ".." || segment === "");
}

/**
 * Campos que denunciam alguém tentando mandar o certificado ou a senha pela API.
 *
 * O A1 e sua senha **nunca** atravessam esta API (invariante 8 da arquitetura
 * fiscal): vivem no cofre do serviço fiscal, e o cadastro guarda só o nome da
 * referência. Um formulário que aceitasse o arquivo "por conveniência" poria o
 * certificado que assina notas em nome da empresa no log do proxy, no corpo da
 * requisição e no backup do Postgres.
 */
const SECRET_LIKE = /(senha|password|passwd|secret_value|pfx|p12|pem|base64|certificate_bytes|private_key)/i;

export function secretLikeFields(body: unknown): string[] {
  if (body === null || typeof body !== "object") return [];
  return Object.keys(body as Record<string, unknown>).filter((key) => SECRET_LIKE.test(key));
}

// --------------------------------------------------------------------------- //
// Configuração da loja
// --------------------------------------------------------------------------- //

export interface FiscalAddress {
  street: string;
  number: string;
  district: string;
  city_code: string;
  city: string;
  zip: string;
}

export interface FiscalConfigInput {
  uf: string;
  environment: "homologation" | "production";
  cnpj: string;
  state_registration: string;
  tax_regime: number;
  legal_name: string;
  address: FiscalAddress;
  certificate_ref: string;
  csc_ref: string;
  csc_id: string;
  enabled: boolean;
}

/** Devolve a lista de problemas; vazia quando o cadastro está coerente. */
export function validateFiscalConfig(
  config: FiscalConfigInput,
  options: {
    productionEnabled: boolean;
    /**
     * Exigir as referências do A1 e do CSC. Falso enquanto a emissão está
     * desligada: o dono salva o que já tem, e referência preenchida continua
     * sendo validada. Para ligar a emissão, é sempre verdadeiro.
     */
    requireCredentials?: boolean;
  },
): string[] {
  const requireCredentials = options.requireCredentials ?? true;
  const problems: string[] = [];
  const ufCode = UF_CODES[config.uf];

  if (!ufCode) problems.push("UF inválida.");
  if (!isValidCnpj(config.cnpj)) problems.push("CNPJ inválido: os dígitos verificadores não conferem.");
  // Pontuação é aceita (é como vem impressa no cartão do contribuinte), mas o
  // que sobra precisa ser só dígito. "ISENTO" não serve: emitente de NFC-e é
  // contribuinte do ICMS e tem inscrição.
  if (!/^\d{2,14}$/.test(config.state_registration.replace(/[.\-/\s]/g, ""))) {
    problems.push("Inscrição estadual precisa conter apenas números.");
  }
  if (!TAX_REGIMES.includes(config.tax_regime as (typeof TAX_REGIMES)[number])) {
    problems.push("Regime tributário (CRT) inválido.");
  }
  if (config.legal_name.trim().length < 2 || config.legal_name.trim().length > 60) {
    problems.push("Razão social precisa ter entre 2 e 60 caracteres.");
  }

  const address = config.address;
  if (!address.street.trim() || !address.number.trim() || !address.district.trim() ||
      !address.city.trim()) {
    problems.push("Endereço incompleto: logradouro, número, bairro e município.");
  }
  if (!/^\d{8}$/.test(digits(address.zip))) problems.push("CEP precisa de 8 dígitos.");
  if (!/^\d{7}$/.test(address.city_code)) {
    problems.push("Código IBGE do município precisa de 7 dígitos.");
  } else if (ufCode && !address.city_code.startsWith(ufCode)) {
    // Município de outra UF: a SEFAZ recusa, e é o erro típico de copiar o
    // código IBGE da loja vizinha do outro lado da divisa.
    problems.push(`O município ${address.city_code} não pertence a ${config.uf}.`);
  }

  const check = (value: string) => requireCredentials || value !== "";
  if (check(config.certificate_ref) && !isValidSecretRef(config.certificate_ref)) {
    problems.push("Referência do certificado A1 inválida (use o nome no cofre, ex.: loja-centro/a1.pfx).");
  }
  // O CSC é opcional: o QR Code v3 (NT 2025.001), padrão do emissor, não o
  // usa. Informado, ele precisa estar certo — é a volta ao v2, se preciso.
  if (config.csc_ref !== "" && !isValidSecretRef(config.csc_ref)) {
    problems.push("Referência do CSC inválida.");
  }
  if (config.csc_id !== "" && !/^\d{1,6}$/.test(config.csc_id)) {
    problems.push("Identificador do CSC precisa de 1 a 6 dígitos.");
  }
  if ((config.csc_ref === "") !== (config.csc_id === "")) {
    problems.push("Informe a referência e o identificador do CSC juntos, ou nenhum dos dois.");
  }

  if (config.environment === "production" && !options.productionEnabled) {
    // A mesma trava que protege a emissão (ver `lib/fiscal/service.ts`),
    // aplicada no cadastro: não adianta deixar salvar uma configuração que
    // seria recusada em toda venda.
    problems.push(
      "Produção ainda não liberada nesta retaguarda: o motor fiscal não foi homologado.",
    );
  }
  return problems;
}

// --------------------------------------------------------------------------- //
// Perfil tributário do produto
// --------------------------------------------------------------------------- //

export interface ProductProfileInput {
  ncm: string;
  cfop: string;
  cest?: string | null;
  unit_code: string;
  origin: number;
  csosn?: string | null;
  cst_icms?: string | null;
  cst_pis: string;
  cst_cofins: string;
}

/**
 * Confere forma e coerência do perfil tributário.
 *
 * @param regimes CRT das lojas do tenant que já têm configuração fiscal. O
 *   perfil do produto é do tenant, e não da loja; se todas as lojas usam o
 *   mesmo regime, o tipo de CST é exigido de acordo. Com regimes misturados
 *   (ou nenhum configurado), qualquer um dos dois é aceito — decidir por loja
 *   exigiria perfis por loja, que o modelo ainda não tem.
 */
export function validateProductProfile(
  profile: ProductProfileInput,
  regimes: number[],
): string[] {
  const problems: string[] = [];

  if (!/^\d{8}$/.test(profile.ncm)) problems.push("NCM precisa de 8 dígitos.");
  if (!/^\d{4}$/.test(profile.cfop)) {
    problems.push("CFOP precisa de 4 dígitos.");
  } else if (!profile.cfop.startsWith("5")) {
    // A NFC-e documenta venda ao consumidor final DENTRO do estado. CFOP de
    // operação interestadual (6xxx) ou de exterior (7xxx) é recusado pela
    // SEFAZ em toda venda deste produto.
    problems.push("CFOP da NFC-e precisa ser de operação interna (5xxx).");
  }
  if (profile.cest && !/^\d{7}$/.test(profile.cest)) problems.push("CEST precisa de 7 dígitos.");
  if (!profile.unit_code.trim() || profile.unit_code.length > 6) {
    problems.push("Unidade comercial precisa de 1 a 6 caracteres (ex.: UN, KG).");
  }
  if (!Number.isInteger(profile.origin) || profile.origin < 0 || profile.origin > 8) {
    problems.push("Origem da mercadoria precisa estar entre 0 e 8.");
  }

  const hasCsosn = Boolean(profile.csosn);
  const hasCst = Boolean(profile.cst_icms);
  if (hasCsosn === hasCst) {
    problems.push("Informe o CSOSN (Simples Nacional/MEI) ou o CST de ICMS — exatamente um.");
  } else if (hasCsosn && !CSOSN_CODES.has(profile.csosn!)) {
    problems.push(`CSOSN ${profile.csosn} não existe.`);
  } else if (hasCst && !CST_ICMS_CODES.has(profile.cst_icms!)) {
    problems.push(`CST de ICMS ${profile.cst_icms} não existe.`);
  }

  const kinds = new Set(regimes.map(usesCsosn));
  if (kinds.size === 1 && hasCsosn !== hasCst) {
    const simples = [...kinds][0];
    if (simples && !hasCsosn) {
      problems.push("As lojas estão no Simples Nacional/MEI: use CSOSN, não CST de ICMS.");
    }
    if (!simples && !hasCst) {
      problems.push("As lojas estão no Regime Normal: use CST de ICMS, não CSOSN.");
    }
  }

  if (!CST_PIS_COFINS_CODES.has(profile.cst_pis)) problems.push(`CST de PIS ${profile.cst_pis} não existe.`);
  if (!CST_PIS_COFINS_CODES.has(profile.cst_cofins)) {
    problems.push(`CST de COFINS ${profile.cst_cofins} não existe.`);
  }
  return problems;
}
