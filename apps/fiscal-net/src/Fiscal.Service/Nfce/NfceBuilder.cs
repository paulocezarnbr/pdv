using System.Globalization;
using System.Text.Json;
using DFe.Classes.Entidades;
using DFe.Classes.Flags;
using NFe.Classes.Informacoes;
using NFe.Classes.Informacoes.Detalhe;
using NFe.Classes.Informacoes.Detalhe.Tributacao;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Estadual;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Estadual.Tipos;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Federal;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Federal.Tipos;
using NFe.Classes.Informacoes.Emitente;
using NFe.Classes.Informacoes.Identificacao;
using NFe.Classes.Informacoes.Identificacao.Tipos;
using NFe.Classes.Informacoes.Pagamento;
using NFe.Classes.Informacoes.Total;
using NFe.Classes.Informacoes.Transporte;
using Shared.NFe.Classes.Informacoes.InfRespTec;
using NfeDocument = NFe.Classes.NFe;

namespace Fiscal.Service.Nfce;

/// <summary>
/// Dado fiscal que não permite montar a nota sem inventar: tributação que o
/// motor ainda não sabe calcular, valores que não fecham, endereço incompleto.
/// </summary>
/// <remarks>A mensagem vai para o painel: diz o que corrigir, sem segredo nenhum.</remarks>
public sealed class FiscalDataException(string message) : Exception(message);

/// <summary>
/// A NFC-e 4.00 a partir do pedido da retaguarda — sem assinatura. Função pura:
/// mesmo pedido, mesmo instante e mesmo <c>cNF</c> produzem a mesma nota.
/// </summary>
/// <remarks>
/// <para>
/// <b>O que o motor emite hoje.</b> ICMS pelo Simples (CSOSN 102, 103, 300,
/// 400, 500) ou regime normal sem destaque (CST 40, 41, 50, 60); PIS e COFINS
/// não tributados (04 a 09) ou "outras" sem base (49, 99). O resto — CSOSN 101
/// e 201+, CST 00/10/20 e PIS/COFINS 01/02 — precisa de alíquota e crédito
/// que o cadastro ainda não guarda, e é recusado com o motivo. O sistema não
/// inventa tributação.
/// </para>
/// <para>
/// <b>Reforma Tributária.</b> O grupo IBS/CBS (NT 2025.002) não é enviado: em
/// 2026 é dispensado para o Simples e informativo para os demais. Os XSDs já o
/// aceitam; ele entra quando o cadastro do produto tiver a classificação.
/// </para>
/// </remarks>
public static class NfceBuilder
{
    /// <summary>Exigência da SEFAZ para o primeiro item em homologação (cStat 373).</summary>
    public const string HomologationDescription = "NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL";

    /// <summary>Brasil sem horário de verão desde 2019: o RJ é sempre -03:00.</summary>
    public static DateTimeOffset BrazilTime(DateTimeOffset utc) => utc.ToOffset(TimeSpan.FromHours(-3));

    /// <summary>Um <c>cNF</c> de 8 dígitos, diferente do número da nota (regra da SEFAZ).</summary>
    public static string RandomCode(long number)
    {
        while (true)
        {
            var code = System.Security.Cryptography.RandomNumberGenerator.GetInt32(0, 100_000_000).ToString("D8", CultureInfo.InvariantCulture);
            if (code != number.ToString("D8", CultureInfo.InvariantCulture)) return code;
        }
    }

    public static NfeDocument Build(FiscalIntent intent, DateTimeOffset issuedAt, string randomCode, TechnicalContact? technical = null)
    {
        var uf = ParseUf(intent.Issuer.Uf);
        var homologation = !intent.IsProduction;
        var address = Address(intent.Issuer, uf);
        var regime = intent.Issuer.TaxRegime switch
        {
            1 => CRT.SimplesNacional,
            2 => CRT.SimplesNacionalExcessoSublimite,
            3 => CRT.RegimeNormal,
            4 => CRT.SimplesNacionalMei,
            var other => throw new FiscalDataException($"Regime tributário {other} desconhecido."),
        };
        var simples = regime != CRT.RegimeNormal;

        var info = new infNFe
        {
            versao = "4.00",
            ide = new ide
            {
                cUF = uf,
                natOp = "VENDA",
                mod = ModeloDocumento.NFCe,
                serie = intent.Series,
                nNF = intent.Number,
                dhEmi = BrazilTime(issuedAt),
                tpNF = TipoNFe.tnSaida,
                idDest = DestinoOperacao.doInterna,
                cMunFG = address.cMun,
                tpImp = TipoImpressao.tiNFCe,
                tpEmis = TipoEmissao.teNormal,
                tpAmb = homologation ? TipoAmbiente.Homologacao : TipoAmbiente.Producao,
                finNFe = FinalidadeNFe.fnNormal,
                indFinal = ConsumidorFinal.cfConsumidorFinal,
                indPres = PresencaComprador.pcPresencial,
                procEmi = ProcessoEmissao.peAplicativoContribuinte,
                verProc = "ERPFood Fiscal 1.0",
                cNF = randomCode,
            },
            emit = new emit
            {
                CNPJ = intent.Issuer.Cnpj,
                xNome = intent.Issuer.LegalName,
                IE = new string(intent.Issuer.StateRegistration.Where(char.IsAsciiDigit).ToArray()),
                CRT = regime,
                enderEmit = address,
            },
            transp = new transp { modFrete = ModalidadeFrete.mfSemFrete },
        };
        if (info.emit.IE.Length == 0) throw new FiscalDataException("Inscrição estadual do emitente sem dígitos.");

        var lines = Lines(intent);
        for (var i = 0; i < intent.Items.Count; i++)
        {
            var item = intent.Items[i];
            var line = lines[i];
            info.det.Add(new det
            {
                nItem = i + 1,
                prod = new prod
                {
                    cProd = item.ProductId,
                    cEAN = "SEM GTIN",
                    xProd = homologation && i == 0 ? HomologationDescription : item.Name.Trim(),
                    NCM = item.Ncm,
                    CEST = item.Cest,
                    CFOP = int.Parse(item.Cfop, CultureInfo.InvariantCulture),
                    uCom = item.UnitCode,
                    qCom = line.Quantity,
                    vUnCom = line.UnitPrice,
                    vProd = line.Gross,
                    vDesc = line.Discount > 0 ? line.Discount : null,
                    cEANTrib = "SEM GTIN",
                    uTrib = item.UnitCode,
                    qTrib = line.Quantity,
                    vUnTrib = line.UnitPrice,
                    indTot = IndicadorTotal.ValorDoItemCompoeTotalNF,
                },
                imposto = new imposto
                {
                    ICMS = new ICMS { TipoICMS = Icms(item, simples) },
                    PIS = new PIS { TipoPIS = Pis(item) },
                    COFINS = new COFINS { TipoCOFINS = Cofins(item) },
                },
            });
        }

        var gross = lines.Sum(line => line.Gross);
        var discount = lines.Sum(line => line.Discount);
        var total = Cents(intent.TotalCents);
        if (gross - discount != total)
        {
            throw new FiscalDataException($"Os itens somam {gross - discount:0.00} e a venda {total:0.00}.");
        }
        info.total = new total
        {
            ICMSTot = new ICMSTot
            {
                vBC = 0, vICMS = 0, vICMSDeson = 0, vFCP = 0, vBCST = 0, vST = 0, vFCPST = 0, vFCPSTRet = 0,
                vProd = gross, vFrete = 0, vSeg = 0, vDesc = discount, vII = 0, vIPI = 0, vIPIDevol = 0,
                vPIS = 0, vCOFINS = 0, vOutro = 0, vNF = total,
            },
        };
        info.pag = [Payments(intent, total)];
        if (technical is not null)
        {
            info.infRespTec = new infRespTec
            {
                CNPJ = technical.Cnpj, xContato = technical.Contact, email = technical.Email, fone = technical.Phone,
            };
        }
        return new NfeDocument { infNFe = info };
    }

    // -- valores ----------------------------------------------------------------

    private sealed record Line(decimal Quantity, decimal UnitPrice, decimal Gross, decimal Discount);

    private static decimal Cents(long cents) => cents / 100m;

    /// <summary>
    /// <c>vProd</c> tem de bater com <c>qCom × vUnCom</c> em até um centavo (regra
    /// 629), e o total da venda sai de <c>vProd − vDesc</c>. O desconto do item é
    /// a diferença entre o preço cheio e o total do item; o desconto do pedido é
    /// rateado pelos itens, proporcional ao total de cada um, sem sobrar centavo.
    /// </summary>
    private static List<Line> Lines(FiscalIntent intent)
    {
        var lines = new List<Line>();
        foreach (var item in intent.Items)
        {
            var quantity = IntentValidator.ParseQuantity(item.Quantity)
                ?? throw new FiscalDataException($"Quantidade inválida em {item.Name}.");
            var unitPrice = Cents(item.UnitPriceCents);
            var exact = quantity * unitPrice;
            var lineTotal = Cents(item.TotalCents);
            if (Math.Abs(exact - lineTotal) <= 0.01m)
            {
                lines.Add(new Line(quantity, unitPrice, lineTotal, 0));
            }
            else if (lineTotal < exact)
            {
                var gross = decimal.Round(exact, 2, MidpointRounding.ToEven);
                lines.Add(new Line(quantity, unitPrice, gross, gross - lineTotal));
            }
            else
            {
                throw new FiscalDataException(
                    $"O total de {item.Name} ({lineTotal:0.00}) passa do preço × quantidade ({exact:0.00}).");
            }
        }

        var net = lines.Sum(line => line.Gross - line.Discount);
        var orderDiscountCents = (long)((net - Cents(intent.TotalCents)) * 100);
        if (orderDiscountCents < 0)
        {
            throw new FiscalDataException("O total da venda passa da soma dos itens: acréscimo não é emitido na NFC-e.");
        }
        if (orderDiscountCents == 0) return lines;

        // Rateio pelo maior resto: a soma dos descontos é exatamente o desconto do pedido.
        var bases = lines.Select(line => (long)((line.Gross - line.Discount) * 100)).ToArray();
        var baseTotal = bases.Sum();
        var shares = bases.Select(b => orderDiscountCents * b / baseTotal).ToArray();
        var remainder = orderDiscountCents - shares.Sum();
        foreach (var index in bases.Select((b, i) => (Leftover: orderDiscountCents * b % baseTotal, Index: i))
                     .OrderByDescending(x => x.Leftover).ThenBy(x => x.Index).Take((int)remainder).Select(x => x.Index))
        {
            shares[index]++;
        }
        return lines.Select((line, i) => line with { Discount = line.Discount + Cents(shares[i]) }).ToList();
    }

    // -- tributação -------------------------------------------------------------

    private static ICMSBasico Icms(FiscalItem item, bool simples)
    {
        var origin = (OrigemMercadoria)item.Origin;
        if (simples)
        {
            if (item.Csosn is null || item.CstIcms is not null)
            {
                throw new FiscalDataException($"{item.Name}: emitente do Simples usa CSOSN, e só ele.");
            }
            return item.Csosn switch
            {
                "102" or "103" or "300" or "400" => new ICMSSN102 { orig = origin, CSOSN = Enum.Parse<Csosnicms>("Csosn" + item.Csosn) },
                "500" => new ICMSSN500 { orig = origin, CSOSN = Csosnicms.Csosn500 },
                _ => throw new FiscalDataException($"{item.Name}: CSOSN {item.Csosn} exige alíquota ou crédito que o cadastro ainda não guarda."),
            };
        }
        if (item.CstIcms is null || item.Csosn is not null)
        {
            throw new FiscalDataException($"{item.Name}: emitente do regime normal usa CST de ICMS, e só ele.");
        }
        return item.CstIcms switch
        {
            "40" => new ICMS40 { orig = origin, CST = Csticms.Cst40 },
            "41" => new ICMS40 { orig = origin, CST = Csticms.Cst41 },
            "50" => new ICMS40 { orig = origin, CST = Csticms.Cst50 },
            "60" => new ICMS60 { orig = origin, CST = Csticms.Cst60 },
            _ => throw new FiscalDataException($"{item.Name}: CST de ICMS {item.CstIcms} exige alíquota que o cadastro ainda não guarda."),
        };
    }

    private static PISBasico Pis(FiscalItem item) => item.CstPis switch
    {
        "04" or "05" or "06" or "07" or "08" or "09" => new PISNT { CST = Enum.Parse<CSTPIS>("pis" + item.CstPis) },
        "49" or "99" => new PISOutr { CST = Enum.Parse<CSTPIS>("pis" + item.CstPis), vBC = 0, pPIS = 0, vPIS = 0 },
        _ => throw new FiscalDataException($"{item.Name}: CST de PIS {item.CstPis} exige alíquota que o cadastro ainda não guarda."),
    };

    private static COFINSBasico Cofins(FiscalItem item) => item.CstCofins switch
    {
        "04" or "05" or "06" or "07" or "08" or "09" => new COFINSNT { CST = Enum.Parse<CSTCOFINS>("cofins" + item.CstCofins) },
        "49" or "99" => new COFINSOutr { CST = Enum.Parse<CSTCOFINS>("cofins" + item.CstCofins), vBC = 0, pCOFINS = 0, vCOFINS = 0 },
        _ => throw new FiscalDataException($"{item.Name}: CST de COFINS {item.CstCofins} exige alíquota que o cadastro ainda não guarda."),
    };

    // -- pagamento --------------------------------------------------------------

    /// <summary>Os métodos do PDV nos códigos <c>tPag</c> da SEFAZ.</summary>
    public static FormaPagamento PaymentCode(string method) => method switch
    {
        "cash" => FormaPagamento.fpDinheiro,
        "credit" => FormaPagamento.fpCartaoCredito,
        "debit" => FormaPagamento.fpCartaoDebito,
        "pix" => FormaPagamento.fpPagamentoInstantaneoPIXDinamico,
        "prepaid" => FormaPagamento.fpCreditoEmLoja,
        "credit_account" => FormaPagamento.fpCartaoDaLoja,
        "cashback" => FormaPagamento.fpProgramadefidelidade,
        _ => throw new FiscalDataException($"Forma de pagamento '{method}' sem correspondência na NFC-e."),
    };

    private static pag Payments(FiscalIntent intent, decimal total)
    {
        var details = new List<detPag>();
        foreach (var payment in intent.Payments!)
        {
            var code = PaymentCode(payment.Method);
            var detail = new detPag { tPag = code, vPag = Cents(payment.AmountCents) };
            // Cartão exige o grupo `card`. Até o provedor de TEF ser escolhido,
            // a credenciadora e a autorização não chegam aqui: "não integrado".
            if (code is FormaPagamento.fpCartaoCredito or FormaPagamento.fpCartaoDebito)
            {
                detail.card = new card { tpIntegra = TipoIntegracaoPagamento.TipNaoIntegrado };
            }
            details.Add(detail);
        }
        var paid = details.Sum(detail => detail.vPag);
        var change = Cents(intent.Payments!.Sum(payment => payment.ChangeCents));
        if (paid - change != total)
        {
            throw new FiscalDataException($"Pagamentos ({paid:0.00}) menos troco ({change:0.00}) não fecham o total ({total:0.00}).");
        }
        var group = new pag { detPag = details };
        if (change > 0) group.vTroco = change;
        return group;
    }

    // -- emitente ---------------------------------------------------------------

    private static Estado ParseUf(string uf) =>
        Enum.TryParse<Estado>(uf, out var parsed) && Enum.IsDefined(parsed)
            ? parsed
            : throw new FiscalDataException($"UF {uf} desconhecida.");

    private static enderEmit Address(Issuer issuer, Estado uf)
    {
        string Text(string name, bool required = true)
        {
            var value = issuer.Address.ValueKind == JsonValueKind.Object && issuer.Address.TryGetProperty(name, out var raw)
                ? raw.ValueKind switch
                {
                    JsonValueKind.String => raw.GetString()!.Trim(),
                    JsonValueKind.Number => raw.GetRawText(),
                    _ => "",
                }
                : "";
            if (required && value.Length == 0) throw new FiscalDataException($"Endereço do emitente sem '{name}'.");
            return value;
        }

        var cityCode = Text("city_code");
        if (!long.TryParse(cityCode, NumberStyles.None, CultureInfo.InvariantCulture, out var cMun) || cityCode.Length != 7)
        {
            throw new FiscalDataException("Código IBGE do município do emitente precisa de 7 dígitos.");
        }
        var zip = new string(Text("zip").Where(char.IsAsciiDigit).ToArray());
        if (zip.Length != 8) throw new FiscalDataException("CEP do emitente precisa de 8 dígitos.");
        var complement = Text("complement", required: false);
        var phone = new string(Text("phone", required: false).Where(char.IsAsciiDigit).ToArray());
        return new enderEmit
        {
            xLgr = Text("street"),
            nro = Text("number"),
            xCpl = complement.Length > 0 ? complement : null,
            xBairro = Text("district"),
            cMun = cMun,
            xMun = Text("city"),
            UF = uf,
            CEP = zip,
            cPais = 1058,
            xPais = "BRASIL",
            fone = phone.Length is >= 6 and <= 14 ? long.Parse(phone, CultureInfo.InvariantCulture) : null,
        };
    }
}
