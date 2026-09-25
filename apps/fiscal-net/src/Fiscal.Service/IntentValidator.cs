using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Fiscal.Service;

/// <summary>
/// As regras do modelo Pydantic do serviço em Python, uma a uma, mais as do
/// grupo de pagamento, que a NFC-e exige e a versão anterior não recebia.
/// </summary>
/// <remarks>
/// Recusar aqui (422) acontece antes de qualquer reserva no serviço: nada é
/// reivindicado, a SEFAZ não vê nada, e a retaguarda mostra o motivo.
/// </remarks>
public static partial class IntentValidator
{
    /// <summary>Os métodos de <c>payments.method</c> que o PDV grava.</summary>
    public static readonly IReadOnlySet<string> PaymentMethods = new HashSet<string>(StringComparer.Ordinal)
    {
        "cash", "debit", "credit", "pix", "prepaid", "credit_account", "cashback",
    };

    public static IReadOnlyList<string> Validate(FiscalIntent? intent)
    {
        var problems = new List<string>();
        if (intent is null)
        {
            problems.Add("corpo: obrigatório");
            return problems;
        }

        void Require(bool ok, string field, string message)
        {
            if (!ok) problems.Add($"{field}: {message}");
        }

        Require(!string.IsNullOrEmpty(intent.DocumentId), "documentId", "obrigatório");
        Require(!string.IsNullOrEmpty(intent.RequestUuid), "requestUuid", "obrigatório");
        Require(!string.IsNullOrEmpty(intent.OrderId), "orderId", "obrigatório");
        Require(!string.IsNullOrEmpty(intent.TenantId), "tenantId", "obrigatório");
        Require(!string.IsNullOrEmpty(intent.StoreId), "storeId", "obrigatório");
        Require(!string.IsNullOrEmpty(intent.DeviceId), "deviceId", "obrigatório");
        Require(intent.Model == 65, "model", "só NFC-e (65)");
        Require(intent.Series is >= 1 and <= 999, "series", "entre 1 e 999");
        Require(intent.Number is > 0 and <= 999_999_999, "number", "entre 1 e 999999999");
        Require(intent.Environment is "homologation" or "production", "environment", "homologation ou production");
        Require(intent.CertificateRef is { Length: >= 1 and <= 200 }, "certificateRef", "entre 1 e 200 caracteres");
        Require(intent.CscRef is null or { Length: >= 1 and <= 200 }, "cscRef", "entre 1 e 200 caracteres");
        Require(intent.CscId is null or { Length: >= 1 and <= 10 }, "cscId", "entre 1 e 10 caracteres");
        Require(intent.TotalCents > 0, "totalCents", "maior que zero");

        var issuer = intent.Issuer;
        if (issuer is null)
        {
            problems.Add("issuer: obrigatório");
        }
        else
        {
            Require(Uf().IsMatch(issuer.Uf ?? ""), "issuer.uf", "duas letras maiúsculas");
            Require(Digits(14).IsMatch(issuer.Cnpj ?? ""), "issuer.cnpj", "14 dígitos");
            Require(issuer.StateRegistration is { Length: >= 2 and <= 20 }, "issuer.stateRegistration", "entre 2 e 20 caracteres");
            Require(issuer.TaxRegime is >= 1 and <= 4, "issuer.taxRegime", "entre 1 e 4");
            Require(issuer.LegalName is { Length: >= 2 and <= 60 }, "issuer.legalName", "entre 2 e 60 caracteres");
            Require(issuer.Address.ValueKind == JsonValueKind.Object, "issuer.address", "objeto");
        }

        if (intent.Items is not { Count: >= 1 and <= 990 })
        {
            problems.Add("items: entre 1 e 990 itens");
        }
        else
        {
            for (var i = 0; i < intent.Items.Count; i++)
            {
                var item = intent.Items[i];
                var at = $"items.{i}";
                Require(!string.IsNullOrEmpty(item.ProductId), $"{at}.productId", "obrigatório");
                Require(item.Name is { Length: >= 1 and <= 120 }, $"{at}.name", "entre 1 e 120 caracteres");
                Require(ParseQuantity(item.Quantity) is > 0m, $"{at}.quantity", "decimal positivo com até 4 casas");
                Require(item.UnitPriceCents >= 0, $"{at}.unitPriceCents", "não negativo");
                Require(item.TotalCents >= 0, $"{at}.totalCents", "não negativo");
                Require(Digits(8).IsMatch(item.Ncm ?? ""), $"{at}.ncm", "8 dígitos");
                Require(Digits(4).IsMatch(item.Cfop ?? ""), $"{at}.cfop", "4 dígitos");
                Require(item.Cest is null || Digits(7).IsMatch(item.Cest), $"{at}.cest", "7 dígitos");
                Require(item.UnitCode is { Length: >= 1 and <= 6 }, $"{at}.unitCode", "entre 1 e 6 caracteres");
                Require(item.Origin is >= 0 and <= 8, $"{at}.origin", "entre 0 e 8");
                Require(item.Csosn is null || Digits(3).IsMatch(item.Csosn), $"{at}.csosn", "3 dígitos");
                Require(item.CstIcms is null || Digits(2).IsMatch(item.CstIcms), $"{at}.cstIcms", "2 dígitos");
                Require(Digits(2).IsMatch(item.CstPis ?? ""), $"{at}.cstPis", "2 dígitos");
                Require(Digits(2).IsMatch(item.CstCofins ?? ""), $"{at}.cstCofins", "2 dígitos");
            }
        }

        if (intent.Payments is not { Count: >= 1 and <= 100 })
        {
            problems.Add("payments: entre 1 e 100 pagamentos (a NFC-e exige o grupo de pagamento)");
        }
        else
        {
            for (var i = 0; i < intent.Payments.Count; i++)
            {
                var payment = intent.Payments[i];
                Require(PaymentMethods.Contains(payment.Method ?? ""), $"payments.{i}.method", "método desconhecido");
                Require(payment.AmountCents > 0, $"payments.{i}.amountCents", "maior que zero");
                Require(payment.ChangeCents >= 0, $"payments.{i}.changeCents", "não negativo");
            }
        }
        return problems;
    }

    /// <summary>A quantidade como a SEFAZ aceita em <c>qCom</c>: decimal positivo, até 4 casas.</summary>
    public static decimal? ParseQuantity(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        if (!decimal.TryParse(text.Trim(), NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var value)) return null;
        return decimal.Round(value, 4) == value ? value : null;
    }

    [GeneratedRegex("^[A-Z]{2}$")]
    private static partial Regex Uf();

    private static Regex Digits(int count) => count switch
    {
        2 => Digits2(), 3 => Digits3(), 4 => Digits4(), 7 => Digits7(), 8 => Digits8(), 14 => Digits14(),
        _ => throw new ArgumentOutOfRangeException(nameof(count)),
    };

    [GeneratedRegex(@"^\d{2}$")] private static partial Regex Digits2();
    [GeneratedRegex(@"^\d{3}$")] private static partial Regex Digits3();
    [GeneratedRegex(@"^\d{4}$")] private static partial Regex Digits4();
    [GeneratedRegex(@"^\d{7}$")] private static partial Regex Digits7();
    [GeneratedRegex(@"^\d{8}$")] private static partial Regex Digits8();
    [GeneratedRegex(@"^\d{14}$")] private static partial Regex Digits14();
}
