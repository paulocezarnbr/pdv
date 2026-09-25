using System.Security.Cryptography;
using System.Security.Cryptography.Xml;
using System.Text;
using System.Xml;
using System.Xml.Schema;
using DFe.Classes.Entidades;
using Fiscal.Service;
using Fiscal.Service.Nfce;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Estadual;
using NFe.Classes.Informacoes.Detalhe.Tributacao.Federal;
using NFe.Classes.Informacoes.Pagamento;
using NFe.Utils.NFe;

namespace Fiscal.Tests;

/// <summary>
/// A NFC-e montada, assinada e validada no XSD oficial — e cada regra de valor
/// que a SEFAZ confere antes de autorizar.
/// </summary>
public sealed class NfceBuilderTests
{
    private static readonly string Schemas = Path.Combine(AppContext.BaseDirectory, "Schemas");
    private static readonly DateTimeOffset Now = new(2026, 9, 25, 21, 0, 0, TimeSpan.Zero);

    private static string Signed(FiscalIntent intent, System.Security.Cryptography.X509Certificates.X509Certificate2 certificate, int qr = 3,
        string? cscId = null, string? csc = null)
    {
        var nfe = NfceBuilder.Build(intent, Now, "12345678");
        var configuration = NfceDocuments.Configuration(Estado.RJ, intent.IsProduction, Schemas, TimeSpan.FromSeconds(5));
        return NfceDocuments.Sign(nfe, configuration, certificate, qr, cscId, csc);
    }

    private static XmlDocument Load(string xml)
    {
        var document = new XmlDocument { PreserveWhitespace = true };
        document.LoadXml(xml);
        return document;
    }

    private static string Value(XmlDocument document, string name) =>
        document.GetElementsByTagName(name, NfceDocuments.Namespace)[0]!.InnerText;

    [Fact]
    public void A_signed_nfce_passes_the_official_schema_and_its_signature_checks()
    {
        using var certificate = TestPki.Create();
        var xml = Signed(Intents.Sample(), certificate);

        var document = Load(xml);
        var signed = new SignedXml(document);
        signed.LoadXml((XmlElement)document.GetElementsByTagName("Signature", SignedXml.XmlDsigNamespaceUrl)[0]!);
        Assert.True(signed.CheckSignature(certificate, verifySignatureOnly: true));

        Assert.Equal("65", Value(document, "mod"));
        Assert.Equal("2", Value(document, "tpAmb"));
        Assert.Equal("2026-09-25T18:00:00-03:00", Value(document, "dhEmi"));
        Assert.Equal("3304557", Value(document, "cMunFG"));
        Assert.Equal("12345678", Value(document, "IE"));
        Assert.Equal("29.00", Value(document, "vNF"));
        Assert.Equal("21.00", Value(document, "vTroco"));
    }

    [Fact]
    public void The_access_key_carries_the_issuer_series_number_and_a_valid_check_digit()
    {
        using var certificate = TestPki.Create();
        var nfe = NfceBuilder.Build(Intents.Sample(number: 1234), Now, "12345678");
        NfceDocuments.Sign(nfe, NfceDocuments.Configuration(Estado.RJ, false, Schemas, TimeSpan.FromSeconds(5)), certificate, 3);
        var access = NfceDocuments.AccessKey(nfe);

        Assert.Equal(44, access.Length);
        Assert.Equal("33", access[..2]);                  // RJ
        Assert.Equal("2609", access[2..6]);               // AAMM da emissão
        Assert.Equal("11222333000181", access[6..20]);    // CNPJ
        Assert.Equal("65", access[20..22]);               // NFC-e
        Assert.Equal("001", access[22..25]);              // série
        Assert.Equal("000001234", access[25..34]);        // número
        Assert.Equal("1", access[34..35]);                // emissão normal
        Assert.Equal("12345678", access[35..43]);         // cNF
        // Módulo 11 com pesos 2..9, da direita para a esquerda.
        var sum = 0;
        for (int i = 42, weight = 2; i >= 0; i--, weight = weight == 9 ? 2 : weight + 1) sum += (access[i] - '0') * weight;
        var digit = 11 - sum % 11;
        Assert.Equal((digit >= 10 ? 0 : digit).ToString(), access[43..]);
    }

    [Fact]
    public void Qr_code_v3_online_is_key_version_and_environment_without_csc()
    {
        using var certificate = TestPki.Create();
        var document = Load(Signed(Intents.Sample(), certificate));
        var qr = Value(document, "qrCode");
        var key = document.GetElementsByTagName("infNFe", NfceDocuments.Namespace)[0]!.Attributes!["Id"]!.Value[3..];

        Assert.Equal($"https://consultadfe.fazenda.rj.gov.br/consultaNFCe/QRCode?p={key}|3|2", qr);
        Assert.Equal("www.fazenda.rj.gov.br/nfce/consulta", Value(document, "urlChave"));
    }

    [Fact]
    public void Qr_code_v2_hash_matches_the_manual_recomputed_here()
    {
        using var certificate = TestPki.Create();
        const string csc = "0123456789ABCDEF0123456789ABCDEF0123";
        var document = Load(Signed(Intents.Sample(), certificate, qr: 2, cscId: "000001", csc: csc));
        var qr = Value(document, "qrCode");
        var parameters = qr[(qr.IndexOf("?p=", StringComparison.Ordinal) + 3)..].Split('|');

        // Manual do DANFE NFC-e, QR Code 2.00: chave|2|tpAmb|cIdToken sem zeros à esquerda|SHA-1(dados + CSC).
        Assert.Equal(["2", "2", "1"], parameters[1..4]);
        var material = string.Join('|', parameters[..4]) + csc;
        Assert.Equal(Convert.ToHexString(SHA1.HashData(Encoding.UTF8.GetBytes(material))), parameters[4].ToUpperInvariant());
    }

    [Fact]
    public void In_homologation_the_first_item_carries_the_sefaz_text_and_in_production_its_own_name()
    {
        var homologation = NfceBuilder.Build(Intents.Sample(), Now, "12345678");
        var production = NfceBuilder.Build(Intents.Sample(environment: "production"), Now, "12345678");
        Assert.Equal(NfceBuilder.HomologationDescription, homologation.infNFe.det[0].prod.xProd);
        Assert.Equal("Fatia de torta", production.infNFe.det[0].prod.xProd);
    }

    [Fact]
    public void The_signed_xml_survives_a_reload_byte_for_byte()
    {
        // A reconciliação retransmite o XML guardado: recarregado no objeto do
        // DFe.NET, ele precisa sair idêntico, senão a assinatura quebra na SEFAZ.
        using var certificate = TestPki.Create();
        var xml = Signed(Intents.Sample(), certificate);
        var again = NfceDocuments.Load(xml).ObterXmlString();
        Assert.Equal(xml, again);
    }

    [Fact]
    public void Item_and_order_discounts_are_split_to_the_cent()
    {
        // Três itens de R$ 10,00, um com R$ 1,00 de desconto no item, e R$ 1,00
        // de desconto no pedido: R$ 28,00 no total.
        var intent = Intents.Sample(
            items: [Intents.Item("Bolo A", "1", 1000, 1000), Intents.Item("Bolo B", "1", 1000, 900), Intents.Item("Bolo C", "1", 1000, 1000)],
            payments: [new FiscalPayment("credit", 2800, 0)],
            totalCents: 2800);

        var nfe = NfceBuilder.Build(intent, Now, "12345678");

        var discounts = nfe.infNFe.det.Select(d => d.prod.vDesc ?? 0).ToList();
        Assert.Equal(1.00m, discounts.Sum() - 1.00m);                  // pedido rateado, sem sobra
        Assert.Equal(2.00m, nfe.infNFe.total.ICMSTot.vDesc);
        Assert.Equal(30.00m, nfe.infNFe.total.ICMSTot.vProd);
        Assert.Equal(28.00m, nfe.infNFe.total.ICMSTot.vNF);
        Assert.All(nfe.infNFe.det, d => Assert.Equal(10.00m, d.prod.vProd));
        Assert.True(discounts[1] >= 1.00m);                            // o desconto do item fica com ele
    }

    [Fact]
    public void A_weighed_item_keeps_vprod_within_a_cent_of_quantity_times_price()
    {
        // 0,345 kg a R$ 89,90: 31,0155 → o PDV fecha 31,02.
        var intent = Intents.Sample(
            items: [Intents.Item("Bolo por quilo", "0.345", 8990, 3102)], payments: [new FiscalPayment("pix", 3102, 0)], totalCents: 3102);
        var nfe = NfceBuilder.Build(intent, Now, "12345678");
        var product = nfe.infNFe.det[0].prod;
        Assert.Equal(0.345m, product.qCom);
        Assert.Equal(31.02m, product.vProd);
        Assert.True(Math.Abs(product.qCom * product.vUnCom - product.vProd) <= 0.01m);
    }

    [Fact]
    public void Payments_map_to_the_sefaz_codes_and_cards_carry_the_card_group()
    {
        var intent = Intents.Sample(
            items: [Intents.Item("Combo", "1", 7000, 7000)],
            payments:
            [
                new FiscalPayment("cash", 1000, 0), new FiscalPayment("credit", 1000, 0), new FiscalPayment("debit", 1000, 0),
                new FiscalPayment("pix", 1000, 0), new FiscalPayment("prepaid", 1000, 0), new FiscalPayment("credit_account", 1000, 0),
                new FiscalPayment("cashback", 1000, 0),
            ],
            totalCents: 7000);
        var details = NfceBuilder.Build(intent, Now, "12345678").infNFe.pag[0].detPag;

        Assert.Equal([1, 3, 4, 17, 21, 5, 19], details.Select(d => (int)d.tPag));
        Assert.Equal([false, true, true, false, false, false, false], details.Select(d => d.card is not null));
        Assert.All(details.Where(d => d.card is not null), d => Assert.Equal(TipoIntegracaoPagamento.TipNaoIntegrado, d.card.tpIntegra));
    }

    [Fact]
    public void Payments_that_do_not_close_the_total_are_refused_before_signing()
    {
        var intent = Intents.Sample(payments: [new FiscalPayment("cash", 2000, 0)]);
        var error = Assert.Throws<FiscalDataException>(() => NfceBuilder.Build(intent, Now, "12345678"));
        Assert.Contains("não fecham o total", error.Message);
    }

    [Theory]
    [InlineData("101", null, "49", "49", "CSOSN 101")]
    [InlineData("201", null, "49", "49", "CSOSN 201")]
    [InlineData("102", null, "01", "49", "CST de PIS 01")]
    [InlineData("102", null, "49", "02", "CST de COFINS 02")]
    [InlineData(null, "40", "49", "49", "Simples usa CSOSN")]
    public void Taxation_the_engine_cannot_compute_is_refused_not_invented(string? csosn, string? cstIcms, string pis, string cofins, string expected)
    {
        var intent = Intents.Sample(items: [Intents.Item("Torta", "2", 1450, 2900, csosn, cstIcms, pis, cofins)]);
        var error = Assert.Throws<FiscalDataException>(() => NfceBuilder.Build(intent, Now, "12345678"));
        Assert.Contains(expected, error.Message);
    }

    [Fact]
    public void Normal_regime_without_icms_highlight_is_emitted()
    {
        var intent = Intents.Sample(taxRegime: 3,
            items: [Intents.Item("Água", "1", 500, 500, csosn: null, cstIcms: "60", cstPis: "04", cstCofins: "04"), Intents.Item("Pão", "1", 2400, 2400, csosn: null, cstIcms: "40", cstPis: "06", cstCofins: "06")]);
        var nfe = NfceBuilder.Build(intent, Now, "12345678");
        Assert.IsType<ICMS60>(nfe.infNFe.det[0].imposto.ICMS.TipoICMS);
        Assert.IsType<ICMS40>(nfe.infNFe.det[1].imposto.ICMS.TipoICMS);
        Assert.IsType<PISNT>(nfe.infNFe.det[0].imposto.PIS.TipoPIS);
        using var certificate = TestPki.Create();
        Assert.NotEmpty(NfceDocuments.Sign(nfe, NfceDocuments.Configuration(Estado.RJ, false, Schemas, TimeSpan.FromSeconds(5)), certificate, 3));
    }

    [Fact]
    public void The_random_code_never_equals_the_number()
    {
        for (var i = 0; i < 2000; i++) Assert.NotEqual("00000007", NfceBuilder.RandomCode(7));
        Assert.Matches("^[0-9]{8}$", NfceBuilder.RandomCode(1));
    }

    [Fact]
    public void The_processed_document_keeps_the_signed_nfe_intact_and_passes_the_proc_schema()
    {
        using var certificate = TestPki.Create();
        var signed = Signed(Intents.Sample(), certificate);
        var key = Load(signed).GetElementsByTagName("infNFe", NfceDocuments.Namespace)[0]!.Attributes!["Id"]!.Value[3..];

        var processed = NfceDocuments.Processed(signed, FakeSefaz.Protocol(key));

        var original = Load(signed).DocumentElement!.OuterXml;
        Assert.Contains(original, processed);
        var schemas = new XmlSchemaSet { XmlResolver = new XmlUrlResolver() };
        schemas.Add(NfceDocuments.Namespace, Path.Combine(Schemas, "procNFe_v4.00.xsd"));
        var errors = new List<string>();
        var document = new XmlDocument { Schemas = schemas };
        document.LoadXml(processed);
        document.Validate((_, e) => errors.Add(e.Message));
        Assert.Empty(errors);

        // A assinatura continua valendo dentro do nfeProc.
        var check = new SignedXml(document);
        check.LoadXml((XmlElement)document.GetElementsByTagName("Signature", SignedXml.XmlDsigNamespaceUrl)[0]!);
        Assert.True(check.CheckSignature(certificate, verifySignatureOnly: true));
    }

    [Fact]
    public void The_protocol_is_cut_from_the_sefaz_answer_as_it_came()
    {
        const string key = "33260911222333000181650010000000011123456789";
        var soap = "<soap:Envelope xmlns:soap=\"http://www.w3.org/2003/05/soap-envelope\"><soap:Body>" +
                   "<nfeResultMsg xmlns=\"http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4\">" +
                   "<retEnviNFe versao=\"4.00\" xmlns=\"http://www.portalfiscal.inf.br/nfe\"><tpAmb>2</tpAmb><cStat>104</cStat>" +
                   FakeSefaz.Protocol(key).Replace(" xmlns=\"http://www.portalfiscal.inf.br/nfe\"", "") +
                   "</retEnviNFe></nfeResultMsg></soap:Body></soap:Envelope>";
        var protocol = ZeusSefazGateway.ProtocolXml(soap);
        Assert.NotNull(protocol);
        Assert.Contains($"<chNFe>{key}</chNFe>", protocol);
        Assert.Contains("<nProt>333260000000001</nProt>", protocol);
        Assert.Null(ZeusSefazGateway.ProtocolXml("<retEnviNFe xmlns=\"http://www.portalfiscal.inf.br/nfe\"><cStat>225</cStat></retEnviNFe>"));
    }
}
