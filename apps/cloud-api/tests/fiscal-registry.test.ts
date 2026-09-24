/**
 * Regras do cadastro fiscal, sem banco.
 *
 * Cada caso aqui é um erro de cadastro que a SEFAZ só acusaria **na venda** —
 * com o cliente no balcão e o número da série já consumido.
 */

import { describe, expect, it } from "vitest";

import {
  isValidCnpj,
  isValidSecretRef,
  secretLikeFields,
  validateFiscalConfig,
  validateProductProfile,
  type FiscalConfigInput,
  type ProductProfileInput,
} from "../src/lib/fiscal/registry.ts";

// Calculados por fora, com implementação independente.
const VALID_CNPJ = "11222333000181";

function config(overrides: Partial<FiscalConfigInput> = {}): FiscalConfigInput {
  return {
    uf: "RJ",
    environment: "homologation",
    cnpj: VALID_CNPJ,
    state_registration: "12345678",
    tax_regime: 1,
    legal_name: "Confeitaria Aurora Ltda",
    address: {
      street: "Rua do Ouvidor", number: "50", district: "Centro",
      city_code: "3304557", city: "Rio de Janeiro", zip: "20040-030",
    },
    certificate_ref: "loja-centro/a1.pfx",
    csc_ref: "loja-centro/csc",
    csc_id: "1",
    enabled: true,
    ...overrides,
  };
}

function profile(overrides: Partial<ProductProfileInput> = {}): ProductProfileInput {
  return {
    ncm: "19059090", cfop: "5102", cest: null, unit_code: "UN", origin: 0,
    csosn: "102", cst_icms: null, cst_pis: "49", cst_cofins: "49",
    ...overrides,
  };
}

describe("CNPJ", () => {
  it("aceita os dígitos verificadores corretos, com ou sem pontuação", () => {
    expect(isValidCnpj(VALID_CNPJ)).toBe(true);
    expect(isValidCnpj("12.345.678/0001-95")).toBe(true);
    expect(isValidCnpj("98765432000198")).toBe(true);
  });

  it("recusa um dígito trocado — tem 14 dígitos e ainda assim é inválido", () => {
    expect(isValidCnpj("11222333000182")).toBe(false);
    expect(isValidCnpj("12345678000190")).toBe(false);
  });

  it("recusa repetição e tamanho errado", () => {
    expect(isValidCnpj("11111111111111")).toBe(false);
    // Zeros fecham o cálculo do DV (soma 0 -> DV 0): só a regra da repetição
    // impede este cadastro.
    expect(isValidCnpj("00000000000000")).toBe(false);
    expect(isValidCnpj("1122233300018")).toBe(false);
  });
});

describe("configuração da loja", () => {
  it("um cadastro coerente não tem problema", () => {
    expect(validateFiscalConfig(config(), { productionEnabled: false })).toEqual([]);
  });

  it("município de outra UF é recusado", () => {
    // O erro típico de copiar o código IBGE da loja do outro lado da divisa.
    const problems = validateFiscalConfig(
      config({ address: { ...config().address, city_code: "3550308" } }), // São Paulo
      { productionEnabled: false },
    );
    expect(problems.join(" ")).toContain("não pertence a RJ");
  });

  it("produção é recusada enquanto o motor não foi homologado", () => {
    const problems = validateFiscalConfig(config({ environment: "production" }), {
      productionEnabled: false,
    });
    expect(problems.join(" ")).toContain("Produção ainda não liberada");
    expect(validateFiscalConfig(config({ environment: "production" }), {
      productionEnabled: true,
    })).toEqual([]);
  });

  it("com a emissão desligada, salva sem as credenciais — mas confere as que vierem", () => {
    const partial = config({ enabled: false, certificate_ref: "", csc_ref: "", csc_id: "" });
    expect(validateFiscalConfig(partial, {
      productionEnabled: false, requireCredentials: false,
    })).toEqual([]);

    const broken = config({ enabled: false, certificate_ref: "../../etc/passwd" });
    expect(validateFiscalConfig(broken, {
      productionEnabled: false, requireCredentials: false,
    }).join(" ")).toContain("certificado");
  });

  it("para ligar a emissão, as credenciais são obrigatórias", () => {
    const problems = validateFiscalConfig(
      config({ certificate_ref: "", csc_ref: "", csc_id: "" }),
      { productionEnabled: false },
    );
    expect(problems).toHaveLength(3);
  });

  it("inscrição estadual 'ISENTO' não serve para emitente de NFC-e", () => {
    const problems = validateFiscalConfig(config({ state_registration: "ISENTO" }), {
      productionEnabled: false,
    });
    expect(problems.join(" ")).toContain("Inscrição estadual");
  });
});

describe("referência de segredo", () => {
  it("aceita nomes relativos do cofre", () => {
    expect(isValidSecretRef("loja-centro/a1.pfx")).toBe(true);
    expect(isValidSecretRef("csc")).toBe(true);
  });

  it("recusa caminho absoluto e travessia", () => {
    for (const ref of ["/etc/passwd", "../a1.pfx", "loja/../../x", "loja//a1", "C:\\a1.pfx"]) {
      expect(isValidSecretRef(ref), ref).toBe(false);
    }
  });

  it("denuncia quem tenta mandar o certificado ou a senha pela API", () => {
    expect(secretLikeFields({ certificate_password: "x", pfx_base64: "y", cnpj: "1" }))
      .toEqual(["certificate_password", "pfx_base64"]);
    expect(secretLikeFields({ senha_a1: "x" })).toEqual(["senha_a1"]);
    expect(secretLikeFields({ cnpj: "1", certificate_ref: "a" })).toEqual([]);
  });
});

describe("perfil tributário do produto", () => {
  it("um perfil coerente do Simples não tem problema", () => {
    expect(validateProductProfile(profile(), [1])).toEqual([]);
  });

  it("CFOP interestadual é recusado — NFC-e é venda dentro do estado", () => {
    expect(validateProductProfile(profile({ cfop: "6102" }), [1]).join(" "))
      .toContain("operação interna");
  });

  it("NCM e CEST precisam do tamanho exato", () => {
    expect(validateProductProfile(profile({ ncm: "1905909" }), [1]).join(" ")).toContain("NCM");
    expect(validateProductProfile(profile({ cest: "123" }), [1]).join(" ")).toContain("CEST");
  });

  it("exige exatamente um entre CSOSN e CST de ICMS", () => {
    expect(validateProductProfile(profile({ csosn: null }), [1]).join(" ")).toContain("exatamente um");
    expect(validateProductProfile(profile({ cst_icms: "00" }), []).join(" ")).toContain("exatamente um");
  });

  it("o tipo de código acompanha o regime da empresa", () => {
    // Simples/MEI tributam ICMS por CSOSN; Regime Normal, por CST.
    expect(validateProductProfile(profile({ csosn: null, cst_icms: "00" }), [1]).join(" "))
      .toContain("use CSOSN");
    expect(validateProductProfile(profile(), [3]).join(" ")).toContain("use CST");
    expect(validateProductProfile(profile({ csosn: null, cst_icms: "00" }), [3])).toEqual([]);
    // MEI (CRT 4) também é Simples para o ICMS.
    expect(validateProductProfile(profile({ csosn: null, cst_icms: "00" }), [4]).join(" "))
      .toContain("use CSOSN");
  });

  it("com regimes misturados entre lojas, aceita os dois tipos", () => {
    expect(validateProductProfile(profile(), [1, 3])).toEqual([]);
    expect(validateProductProfile(profile({ csosn: null, cst_icms: "00" }), [1, 3])).toEqual([]);
  });

  it("código que não existe é recusado", () => {
    expect(validateProductProfile(profile({ csosn: "999" }), [1]).join(" ")).toContain("CSOSN 999");
    expect(validateProductProfile(profile({ cst_pis: "48" }), [1]).join(" ")).toContain("PIS 48");
  });
});
