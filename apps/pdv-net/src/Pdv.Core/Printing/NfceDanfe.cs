using System.Globalization;

namespace Pdv.Core.Printing;

public sealed class DanfeException(string message) : Exception(message);

public sealed record DanfeIssuer(string LegalName, string Cnpj, string StateRegistration, string Address);

/// <summary>Item como está no documento fiscal. A quantidade é decimal ("0.847").</summary>
public sealed record DanfeItem(string Code, string Description, decimal Quantity, string Unit, long UnitPriceCents, long TotalCents);

public sealed record DanfePayment(string Method, long AmountCents);

/// <summary>Tudo o que vai no papel, vindo do documento fiscal — nada calculado aqui.</summary>
/// <param name="Environment">"homologation" ou "production".</param>
/// <param name="Emission">"normal" ou "offline_contingency".</param>
/// <param name="IssuedLocal">Emissão, já no fuso do caixa.</param>
/// <param name="ConsultationUrl"><c>infNFeSupl/urlChave</c> do XML — varia por UF e ambiente.</param>
/// <param name="QrCode"><c>infNFeSupl/qrCode</c> do XML, íntegro. Nunca montado pelo PDV.</param>
public sealed record NfceDanfe(
    DanfeIssuer Issuer,
    string Environment,
    string Emission,
    int Series,
    long Number,
    DateTime IssuedLocal,
    IReadOnlyList<DanfeItem> Items,
    IReadOnlyList<DanfePayment> Payments,
    string AccessKey,
    string ConsultationUrl,
    string QrCode,
    long DiscountCents = 0,
    long ChangeCents = 0,
    string? Protocol = null,
    DateTime? AuthorizedLocal = null,
    string? ConsumerDocument = null,
    long? ApproximateTaxesCents = null)
{
    public long ItemsTotalCents => Items.Sum(item => item.TotalCents);

    public long PayableCents => ItemsTotalCents - DiscountCents;
}

/// <summary>O DANFE NFC-e em 80 mm — o <c>fiscal/danfe.py</c> do Python, byte a byte e recusa a recusa.</summary>
/// <remarks>
/// <para>
/// Imprime o que veio do documento fiscal e <b>recusa</b> o documento
/// incoerente. A chave de acesso carrega o documento dentro dela (UF, AAMM,
/// CNPJ, modelo, série, número, tipo de emissão, código e DV), então dá para
/// conferir sem consultar ninguém que o papel é deste emitente, desta série e
/// deste número. Um DANFE "normal" sem protocolo afirmaria uma autorização
/// que não aconteceu — pior, na fiscalização, que não entregar nada.
/// </para>
/// <para>Conferido contra <c>contracts/danfe.json</c>, gerado pelo Python.</para>
/// </remarks>
public static class NfceDanfeLayout
{
    private const string ModelNfce = "65";

    private static readonly Dictionary<string, char> EmissionCode = new(StringComparer.Ordinal)
    {
        ["normal"] = '1',
        ["offline_contingency"] = '9',
    };

    /// <summary>Dígito verificador da chave: módulo 11, pesos 2 a 9 da direita para a esquerda; resto 0 ou 1 dá 0.</summary>
    public static char CheckDigit(string first43)
    {
        if (first43.Length != 43 || !first43.All(char.IsAsciiDigit)) throw new DanfeException("A base da chave de acesso precisa de 43 dígitos.");
        var total = 0;
        var weight = 2;
        for (var i = first43.Length - 1; i >= 0; i--)
        {
            total += (first43[i] - '0') * weight;
            weight = weight == 9 ? 2 : weight + 1;
        }
        var remainder = total % 11;
        return remainder < 2 ? '0' : (char)('0' + (11 - remainder));
    }

    /// <summary>Recusa o que não pode ir para a mão do consumidor, com o motivo para quem vai corrigir.</summary>
    public static void Validate(NfceDanfe danfe)
    {
        var key = danfe.AccessKey;
        if (key.Length != 44 || !key.All(char.IsAsciiDigit)) throw new DanfeException("A chave de acesso precisa ter 44 dígitos.");
        if (CheckDigit(key[..43]) != key[43]) throw new DanfeException("O dígito verificador da chave de acesso não confere.");

        var cnpj = new string(danfe.Issuer.Cnpj.Where(char.IsAsciiDigit).ToArray());
        if (key[6..20] != cnpj) throw new DanfeException("A chave de acesso é de outro CNPJ, não deste emitente.");
        if (key[20..22] != ModelNfce) throw new DanfeException("A chave de acesso não é de uma NFC-e (modelo 65).");
        if (int.Parse(key[22..25], CultureInfo.InvariantCulture) != danfe.Series ||
            long.Parse(key[25..34], CultureInfo.InvariantCulture) != danfe.Number)
        {
            throw new DanfeException("A série ou o número não conferem com a chave de acesso.");
        }
        if (!EmissionCode.TryGetValue(danfe.Emission, out var code) || key[34] != code)
        {
            throw new DanfeException("O tipo de emissão impresso não confere com o da chave de acesso.");
        }

        if (danfe.Emission == "normal")
        {
            if (string.IsNullOrEmpty(danfe.Protocol) || danfe.AuthorizedLocal is null)
            {
                throw new DanfeException("Emissão normal sem protocolo de autorização não vira DANFE.");
            }
        }
        else if (!string.IsNullOrEmpty(danfe.Protocol))
        {
            throw new DanfeException("Documento em contingência não tem protocolo; os dados se contradizem.");
        }

        if (danfe.Items.Count == 0) throw new DanfeException("O documento não tem itens.");
        if (string.IsNullOrWhiteSpace(danfe.QrCode) || string.IsNullOrWhiteSpace(danfe.ConsultationUrl))
        {
            throw new DanfeException("QR Code ou URL de consulta ausentes no documento fiscal.");
        }

        var paid = danfe.Payments.Sum(payment => payment.AmountCents);
        if (paid - danfe.ChangeCents != danfe.PayableCents)
        {
            throw new DanfeException(
                $"Os pagamentos ({EscPosBuilder.FormatCents(paid - danfe.ChangeCents)}) não fecham com o valor a pagar " +
                $"({EscPosBuilder.FormatCents(danfe.PayableCents)}).");
        }
    }

    /// <summary>Valida e monta. Documento incoerente não imprime.</summary>
    public static byte[] Build(NfceDanfe danfe, PrinterLayout layout)
    {
        Validate(danfe);
        var b = new EscPosBuilder(layout.Columns, layout.Codepage).Initialize();
        Issuer(b, danfe);
        Identification(b);
        Items(b, danfe);
        Totals(b, danfe);
        FiscalMessage(b, danfe);
        Consumer(b, danfe);
        b.Feed(1).AlignTo(Align.Center);
        b.QrCode(danfe.QrCode, moduleSize: 4);
        b.AlignTo(Align.Left);
        Authorization(b, danfe);
        Taxes(b, danfe);
        b.Feed(1);
        b.Cut(layout.CutFeedLines, partial: true);
        return b.Build();
    }

    private static void Issuer(EscPosBuilder b, NfceDanfe danfe)
    {
        b.AlignTo(Align.Center).Bold(true);
        foreach (var line in Wrap(danfe.Issuer.LegalName, b.Columns)) b.Line(line);
        b.Bold(false);
        b.Line($"CNPJ: {FormatCnpj(danfe.Issuer.Cnpj)}  IE: {danfe.Issuer.StateRegistration}");
        foreach (var line in Wrap(danfe.Issuer.Address, b.Columns)) b.Line(line);
        b.AlignTo(Align.Left);
        b.Separator();
    }

    private static void Identification(EscPosBuilder b)
    {
        b.AlignTo(Align.Center);
        b.Line("DANFE NFC-e - Documento Auxiliar");
        b.Line("da Nota Fiscal de Consumidor Eletrônica");
        b.AlignTo(Align.Left);
        b.Separator();
    }

    /// <summary>Duas linhas por item: a descrição é o que o consumidor lê para conferir, e não pode ser truncada pelos números.</summary>
    private static void Items(EscPosBuilder b, NfceDanfe danfe)
    {
        b.Bold(true).Line("Código  Descrição").Bold(false);
        b.Columns2("Qtde Un x Vl Unit", "Vl Total");
        b.Separator();
        foreach (var item in danfe.Items)
        {
            var code = EscPosBuilder.Slice(item.Code, 7);
            code += new string(' ', 7 - EscPosBuilder.Length(code));
            b.Line(EscPosBuilder.Slice($"{code} {item.Description}", b.Columns));
            b.Columns2($"   {Quantity(item.Quantity)} {item.Unit} x {EscPosBuilder.FormatCents(item.UnitPriceCents)}",
                EscPosBuilder.FormatCents(item.TotalCents));
        }
        b.Separator();
    }

    private static void Totals(EscPosBuilder b, NfceDanfe danfe)
    {
        b.Columns2("Qtde. total de itens", danfe.Items.Count.ToString(CultureInfo.InvariantCulture));
        b.Columns2("Valor total R$", EscPosBuilder.FormatCents(danfe.ItemsTotalCents));
        if (danfe.DiscountCents != 0) b.Columns2("Desconto R$", "-" + EscPosBuilder.FormatCents(danfe.DiscountCents));
        b.Bold(true);
        b.Columns2("Valor a Pagar R$", EscPosBuilder.FormatCents(danfe.PayableCents));
        b.Bold(false);
        b.Columns2("FORMA PAGAMENTO", "VALOR PAGO R$");
        foreach (var payment in danfe.Payments)
        {
            b.Columns2(ReceiptLayout.MethodLabels.GetValueOrDefault(payment.Method, payment.Method), EscPosBuilder.FormatCents(payment.AmountCents));
        }
        if (danfe.ChangeCents != 0) b.Columns2("Troco R$", EscPosBuilder.FormatCents(danfe.ChangeCents));
        b.Separator();
    }

    /// <summary>Divisão V — é aqui que o papel diz o que ele é: homologação não vale nada, contingência ainda não foi autorizada.</summary>
    private static void FiscalMessage(EscPosBuilder b, NfceDanfe danfe)
    {
        b.AlignTo(Align.Center);
        if (danfe.Environment == "homologation")
        {
            b.Bold(true);
            b.Line("EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO");
            b.Line("SEM VALOR FISCAL");
            b.Bold(false);
        }
        if (danfe.Emission == "offline_contingency")
        {
            b.Bold(true).Size(1, 2);
            b.Line("EMITIDA EM CONTINGÊNCIA");
            b.Size(1, 1);
            b.Line("Pendente de autorização");
            b.Bold(false);
        }
        b.Line($"Número {danfe.Number:000000000}  Série {danfe.Series:000}  {Stamp(danfe.IssuedLocal)}");
        b.Feed(1);
        b.Line("Consulte pela Chave de Acesso em");
        foreach (var line in Wrap(danfe.ConsultationUrl, b.Columns)) b.Line(line);
        foreach (var line in FormatAccessKey(danfe.AccessKey)) b.Line(line);
        b.AlignTo(Align.Left);
        b.Separator();
    }

    private static void Consumer(EscPosBuilder b, NfceDanfe danfe)
    {
        b.AlignTo(Align.Center);
        b.Line(string.IsNullOrEmpty(danfe.ConsumerDocument)
            ? "CONSUMIDOR NÃO IDENTIFICADO"
            : $"CONSUMIDOR - {FormatDocument(danfe.ConsumerDocument)}");
        b.AlignTo(Align.Left);
    }

    private static void Authorization(EscPosBuilder b, NfceDanfe danfe)
    {
        if (danfe.Emission != "normal" || danfe.AuthorizedLocal is not { } authorized) return;
        b.AlignTo(Align.Center);
        b.Line($"Protocolo de autorização: {danfe.Protocol}");
        b.Line($"Data de autorização: {Stamp(authorized)}");
        b.AlignTo(Align.Left);
    }

    private static void Taxes(EscPosBuilder b, NfceDanfe danfe)
    {
        if (danfe.ApproximateTaxesCents is not { } taxes) return;
        b.Separator();
        b.Line("Tributos Totais Incidentes");
        b.Columns2("(Lei Federal 12.741/2012) R$", EscPosBuilder.FormatCents(taxes));
    }

    // -- formatação ------------------------------------------------------------

    private static string Stamp(DateTime local) => local.ToString("dd/MM/yyyy HH:mm:ss", CultureInfo.InvariantCulture);

    /// <summary>Grupos de quatro em duas linhas: numa só (54 caracteres) a impressora quebraria no meio de um grupo.</summary>
    public static IReadOnlyList<string> FormatAccessKey(string key)
    {
        var groups = Enumerable.Range(0, (key.Length + 3) / 4).Select(i => key.Substring(i * 4, Math.Min(4, key.Length - i * 4))).ToList();
        return [string.Join(' ', groups.Take(6)), string.Join(' ', groups.Skip(6))];
    }

    public static string FormatCnpj(string cnpj)
    {
        var digits = new string(cnpj.Where(char.IsAsciiDigit).ToArray());
        return digits.Length != 14 ? cnpj : $"{digits[..2]}.{digits[2..5]}.{digits[5..8]}/{digits[8..12]}-{digits[12..]}";
    }

    private static string FormatDocument(string document)
    {
        var digits = new string(document.Where(char.IsAsciiDigit).ToArray());
        return digits.Length switch
        {
            11 => $"CPF: {digits[..3]}.{digits[3..6]}.{digits[6..9]}-{digits[9..]}",
            14 => $"CNPJ: {FormatCnpj(digits)}",
            _ => document,
        };
    }

    /// <summary>Sem zeros à direita, com vírgula: 2, 0,847 — nunca 2.000.</summary>
    private static string Quantity(decimal value) =>
        value.ToString("0.############################", CultureInfo.InvariantCulture).Replace('.', ',');

    /// <summary>Quebra por palavra; a palavra maior que a linha (uma URL) é cortada em pedaços.</summary>
    private static List<string> Wrap(string text, int width)
    {
        var lines = new List<string>();
        var current = "";
        foreach (var word in text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = (current + " " + word).Trim();
            if (EscPosBuilder.Length(candidate) <= width)
            {
                current = candidate;
                continue;
            }
            if (current.Length > 0) lines.Add(current);
            var rest = word;
            while (EscPosBuilder.Length(rest) > width)
            {
                lines.Add(EscPosBuilder.Slice(rest, width));
                rest = string.Concat(rest.EnumerateRunes().Skip(width).Select(rune => rune.ToString()));
            }
            current = rest;
        }
        if (current.Length > 0) lines.Add(current);
        return lines.Count > 0 ? lines : [""];
    }
}
