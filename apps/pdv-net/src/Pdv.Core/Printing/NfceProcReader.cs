using System.Globalization;
using System.Xml;
using System.Xml.Linq;

namespace Pdv.Core.Printing;

/// <summary>
/// O <c>nfeProc</c> autorizado, que a retaguarda devolve, transcrito para o DANFE.
/// </summary>
/// <remarks>
/// <para>
/// <b>O DANFE não calcula nem monta nada.</b> Cada campo do papel sai de um
/// elemento do XML autorizado — é ele que tem valor fiscal, e um total
/// recalculado aqui poderia divergir do que a SEFAZ autorizou sem que ninguém
/// notasse. O que não vier no XML, ou vier num formato que exigiria
/// arredondar, é recusado: DANFE errado é pior que DANFE nenhum.
/// </para>
/// <para>
/// Regra a mais que o Python, que nunca imprimiu a partir do XML: o Python só
/// usa <c>fiscal/cloud.py</c> em teste. Conferido contra
/// <c>contracts/nfce-proc.json</c>, uma nota gerada pelo motor do
/// <c>fiscal-net</c>.
/// </para>
/// </remarks>
public static class NfceProcReader
{
    public const string Namespace = "http://www.portalfiscal.inf.br/nfe";

    /// <summary>Uma NFC-e tem alguns kB; um megabyte já é defeito, ou alguém tentando derrubar o caixa.</summary>
    public const int MaxLength = 1024 * 1024;

    /// <summary>
    /// <c>tPag</c> → forma de pagamento do PDV: o inverso do
    /// <c>NfceBuilder.PaymentCode</c> do <c>fiscal-net</c>, exigido igual pelo contrato.
    /// </summary>
    public static readonly IReadOnlyDictionary<string, string> PaymentMethods = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["01"] = "cash",
        ["03"] = "credit",
        ["04"] = "debit",
        ["17"] = "pix",
        ["21"] = "prepaid",
        ["05"] = "credit_account",
        ["19"] = "cashback",
    };

    /// <summary>Status de uso autorizado: 100, e 150 (autorizado fora de prazo).</summary>
    private static readonly HashSet<string> AuthorizedStatus = ["100", "150"];

    private static readonly XNamespace Nfe = Namespace;

    /// <summary>Lê, confere e devolve o DANFE pronto para <see cref="NfceDanfeLayout.Build"/>.</summary>
    /// <param name="processedXml">O <c>nfeProc</c>: NFe assinada + <c>protNFe</c>.</param>
    /// <param name="local">O fuso do caixa, para as datas impressas.</param>
    public static NfceDanfe Read(string processedXml, TimeZoneInfo local)
    {
        if (string.IsNullOrWhiteSpace(processedXml)) throw new DanfeException("A retaguarda não devolveu o XML da nota.");
        if (processedXml.Length > MaxLength) throw new DanfeException("O XML da nota é grande demais para ser uma NFC-e.");

        var root = Parse(processedXml);
        if (root.Name != Nfe + "nfeProc") throw new DanfeException("O XML não é um nfeProc (nota com protocolo).");

        var info = Required(Required(root, "NFe"), "infNFe");
        var protocol = Required(Required(root, "protNFe"), "infProt");

        var status = Text(protocol, "cStat");
        if (!AuthorizedStatus.Contains(status))
        {
            throw new DanfeException($"A nota não está autorizada (cStat {status}): não vira DANFE.");
        }

        var id = (string?)info.Attribute("Id") ?? "";
        if (!id.StartsWith("NFe", StringComparison.Ordinal)) throw new DanfeException("A nota não tem a chave de acesso no Id.");
        var accessKey = id[3..];
        if (Text(protocol, "chNFe") != accessKey)
        {
            throw new DanfeException("O protocolo é de outra nota: a chave não confere.");
        }

        var ide = Required(info, "ide");
        if (Text(ide, "mod") != "65") throw new DanfeException("O documento não é uma NFC-e (modelo 65).");
        var environment = Text(ide, "tpAmb") switch
        {
            "1" => "production",
            "2" => "homologation",
            var other => throw new DanfeException($"Ambiente {other} desconhecido."),
        };
        var emission = Text(ide, "tpEmis") switch
        {
            "1" => "normal",
            "9" => "offline_contingency",
            var other => throw new DanfeException($"Tipo de emissão {other} não é impresso por este caixa."),
        };

        var emit = Required(info, "emit");
        var issuer = new DanfeIssuer(Text(emit, "xNome"), Text(emit, "CNPJ"), Text(emit, "IE"), Address(Required(emit, "enderEmit")));

        var items = info.Elements(Nfe + "det").Select(det =>
        {
            var prod = Required(det, "prod");
            return new DanfeItem(
                Text(prod, "cProd"), Text(prod, "xProd"), Decimal(prod, "qCom"), Text(prod, "uCom"),
                Cents(prod, "vUnCom"), Cents(prod, "vProd"));
        }).ToList();

        var totals = Required(Required(info, "total"), "ICMSTot");
        var pag = Required(info, "pag");
        var payments = pag.Elements(Nfe + "detPag").Select(detail =>
        {
            var code = Text(detail, "tPag");
            if (!PaymentMethods.TryGetValue(code, out var method))
            {
                throw new DanfeException($"Forma de pagamento tPag {code} desconhecida por este caixa.");
            }
            return new DanfePayment(method, Cents(detail, "vPag"));
        }).ToList();

        var supplement = Required(root.Element(Nfe + "NFe")!, "infNFeSupl");
        var dest = info.Element(Nfe + "dest");
        var consumer = dest is null ? null : (Optional(dest, "CPF") ?? Optional(dest, "CNPJ"));

        var danfe = new NfceDanfe(
            issuer, environment, emission,
            int.Parse(Text(ide, "serie"), NumberStyles.None, CultureInfo.InvariantCulture),
            long.Parse(Text(ide, "nNF"), NumberStyles.None, CultureInfo.InvariantCulture),
            Local(Text(ide, "dhEmi"), local),
            items, payments, accessKey,
            Text(supplement, "urlChave"), Text(supplement, "qrCode"),
            DiscountCents: Cents(totals, "vDesc"),
            ChangeCents: pag.Element(Nfe + "vTroco") is null ? 0 : Cents(pag, "vTroco"),
            Protocol: Text(protocol, "nProt"),
            AuthorizedLocal: Local(Text(protocol, "dhRecbto"), local),
            ConsumerDocument: consumer,
            ApproximateTaxesCents: totals.Element(Nfe + "vTotTrib") is null ? null : Cents(totals, "vTotTrib"));

        // O total do papel é o do XML: o que o DANFE soma (itens − desconto)
        // precisa bater com o vNF autorizado, senão alguma coisa foi lida errada.
        if (danfe.PayableCents != Cents(totals, "vNF"))
        {
            throw new DanfeException("Itens menos desconto não fecham com o valor da nota (vNF).");
        }
        NfceDanfeLayout.Validate(danfe);
        return danfe;
    }

    /// <summary>"Rua do Ouvidor, 50 - Centro - Rio de Janeiro/RJ - CEP 20040-030".</summary>
    public static string Address(XElement address)
    {
        var street = $"{Text(address, "xLgr")}, {Text(address, "nro")}";
        if (Optional(address, "xCpl") is { } complement) street += $" {complement}";
        var zip = Text(address, "CEP");
        var formattedZip = zip.Length == 8 ? $"{zip[..5]}-{zip[5..]}" : zip;
        return $"{street} - {Text(address, "xBairro")} - {Text(address, "xMun")}/{Text(address, "UF")} - CEP {formattedZip}";
    }

    private static XElement Parse(string xml)
    {
        // Sem DTD e sem resolver: o XML vem da rede, e uma entidade externa
        // faria o caixa ler arquivo local ou abrir conexão por conta própria.
        var settings = new XmlReaderSettings
        {
            DtdProcessing = DtdProcessing.Prohibit,
            XmlResolver = null,
            MaxCharactersInDocument = MaxLength,
        };
        try
        {
            using var reader = XmlReader.Create(new StringReader(xml), settings);
            return XDocument.Load(reader).Root ?? throw new DanfeException("O XML da nota está vazio.");
        }
        catch (XmlException error)
        {
            throw new DanfeException($"O XML da nota é ilegível: {error.Message}");
        }
    }

    private static XElement Required(XElement parent, string name) =>
        parent.Element(Nfe + name) ?? throw new DanfeException($"O XML da nota não tem {name}.");

    private static string Text(XElement parent, string name)
    {
        var value = Required(parent, name).Value.Trim();
        return value.Length > 0 ? value : throw new DanfeException($"O XML da nota tem {name} vazio.");
    }

    private static string? Optional(XElement parent, string name)
    {
        var value = parent.Element(Nfe + name)?.Value.Trim();
        return string.IsNullOrEmpty(value) ? null : value;
    }

    private static decimal Decimal(XElement parent, string name)
    {
        var text = Text(parent, name);
        return decimal.TryParse(text, NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var value)
            ? value
            : throw new DanfeException($"{name} não é um número: {text}.");
    }

    /// <summary>Reais com até dois decimais em centavos — nunca arredondando.</summary>
    private static long Cents(XElement parent, string name)
    {
        var value = Decimal(parent, name) * 100;
        if (value != decimal.Truncate(value))
        {
            throw new DanfeException($"{name} tem fração de centavo; o caixa não arredonda o que a nota diz.");
        }
        return (long)value;
    }

    private static DateTime Local(string text, TimeZoneInfo local) =>
        DateTimeOffset.TryParseExact(text, "yyyy-MM-ddTHH:mm:sszzz", CultureInfo.InvariantCulture, DateTimeStyles.None, out var stamp)
            ? TimeZoneInfo.ConvertTime(stamp, local).DateTime
            : throw new DanfeException($"Data da nota fora do formato: {text}.");
}
