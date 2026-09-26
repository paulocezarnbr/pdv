using System.Globalization;
using System.Text.Json;
using Pdv.Core.Printing;

namespace Pdv.Core.Tests;

/// <summary>O DANFE NFC-e contra <c>contracts/danfe.json</c>, gerado pelo Python: o papel e as recusas.</summary>
public sealed class DanfeContractTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("danfe.json"))).RootElement.Clone();

    private static NfceDanfe Read(JsonElement spec)
    {
        var issuer = spec.GetProperty("issuer");
        string? Text(JsonElement element, string name) =>
            element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
        DateTime? Local(string name) => Text(spec, name) is { } text
            ? DateTime.ParseExact(text, "yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture)
            : null;
        return new NfceDanfe(
            new DanfeIssuer(Text(issuer, "legal_name")!, Text(issuer, "cnpj")!, Text(issuer, "state_registration")!, Text(issuer, "address")!),
            Text(spec, "environment")!, Text(spec, "emission")!, spec.GetProperty("series").GetInt32(), spec.GetProperty("number").GetInt64(),
            Local("issued_local") ?? DateTime.MinValue,
            spec.GetProperty("items").EnumerateArray().Select(item => new DanfeItem(
                Text(item, "code")!, Text(item, "description")!,
                decimal.Parse(Text(item, "quantity")!, CultureInfo.InvariantCulture), Text(item, "unit")!,
                item.GetProperty("unit_price_cents").GetInt64(), item.GetProperty("total_cents").GetInt64())).ToList(),
            spec.GetProperty("payments").EnumerateArray().Select(payment => new DanfePayment(
                Text(payment, "method")!, payment.GetProperty("amount_cents").GetInt64())).ToList(),
            Text(spec, "access_key")!, Text(spec, "consultation_url")!, Text(spec, "qr_code")!,
            spec.GetProperty("discount_cents").GetInt64(), spec.GetProperty("change_cents").GetInt64(),
            Text(spec, "protocol"), Local("authorized_local"), Text(spec, "consumer_document"),
            spec.GetProperty("approximate_taxes_cents").ValueKind == JsonValueKind.Number
                ? spec.GetProperty("approximate_taxes_cents").GetInt64()
                : null);
    }

    public static TheoryData<int> Valid() => [.. Enumerable.Range(0, Contract.GetProperty("valid").GetArrayLength())];

    public static TheoryData<int> Invalid() => [.. Enumerable.Range(0, Contract.GetProperty("invalid").GetArrayLength())];

    [Theory]
    [MemberData(nameof(Valid))]
    public void A_coherent_document_prints_the_pythons_bytes(int index)
    {
        var spec = Contract.GetProperty("valid")[index];
        Assert.Equal(spec.GetProperty("hex").GetString(),
            Convert.ToHexString(NfceDanfeLayout.Build(Read(spec), new PrinterLayout())).ToLowerInvariant());
    }

    [Theory]
    [MemberData(nameof(Invalid))]
    public void An_incoherent_document_is_refused_with_the_pythons_reason(int index)
    {
        var spec = Contract.GetProperty("invalid")[index];
        var error = Assert.Throws<DanfeException>(() => NfceDanfeLayout.Build(Read(spec), new PrinterLayout()));
        Assert.Equal(spec.GetProperty("error").GetString(), error.Message);
    }

    [Fact]
    public void Check_digit_key_lines_and_cnpj_are_the_pythons()
    {
        foreach (var entry in Contract.GetProperty("check_digits").EnumerateObject())
        {
            Assert.Equal(entry.Value.GetString()![0], NfceDanfeLayout.CheckDigit(entry.Name));
        }
        var key = Contract.GetProperty("valid")[0].GetProperty("access_key").GetString()!;
        Assert.Equal(Contract.GetProperty("access_key_lines").EnumerateArray().Select(line => line.GetString()), NfceDanfeLayout.FormatAccessKey(key));
        foreach (var entry in Contract.GetProperty("cnpj").EnumerateObject())
        {
            Assert.Equal(entry.Value.GetString(), NfceDanfeLayout.FormatCnpj(entry.Name));
        }
    }
}
