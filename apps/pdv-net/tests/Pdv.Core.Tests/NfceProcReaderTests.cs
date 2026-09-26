using System.Globalization;
using System.Text;
using System.Text.Json;
using Pdv.Core.Printing;

namespace Pdv.Core.Tests;

/// <summary>
/// O DANFE a partir do <c>nfeProc</c> contra <c>contracts/nfce-proc.json</c> —
/// uma nota autorizada saída do motor do <c>fiscal-net</c>.
/// </summary>
public sealed class NfceProcReaderTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("nfce-proc.json"))).RootElement.Clone();

    private static string Xml => Contract.GetProperty("xml").GetString()!;

    private static JsonElement Expected => Contract.GetProperty("expected");

    /// <summary>O fuso do Rio sem depender do banco de fusos da máquina: UTC−3, sem horário de verão desde 2019.</summary>
    private static readonly TimeZoneInfo Rio = TimeZoneInfo.CreateCustomTimeZone("Rio", TimeSpan.FromHours(-3), "Rio", "Rio");

    private static NfceDanfe Read(string? xml = null) => NfceProcReader.Read(xml ?? Xml, Rio);

    private static string Tamper(string from, string to)
    {
        Assert.Contains(from, Xml);
        return Xml.Replace(from, to, StringComparison.Ordinal);
    }

    [Fact]
    public void Every_field_comes_from_the_authorized_note()
    {
        var danfe = Read();

        Assert.Equal(Expected.GetProperty("access_key").GetString(), danfe.AccessKey);
        Assert.Equal(Expected.GetProperty("protocol").GetString(), danfe.Protocol);
        Assert.Equal(Expected.GetProperty("environment").GetString(), danfe.Environment);
        Assert.Equal(Expected.GetProperty("emission").GetString(), danfe.Emission);
        Assert.Equal(Expected.GetProperty("series").GetInt32(), danfe.Series);
        Assert.Equal(Expected.GetProperty("number").GetInt64(), danfe.Number);
        Assert.Equal(Expected.GetProperty("legal_name").GetString(), danfe.Issuer.LegalName);
        Assert.Equal(Expected.GetProperty("cnpj").GetString(), danfe.Issuer.Cnpj);
        Assert.Equal(Expected.GetProperty("state_registration").GetString(), danfe.Issuer.StateRegistration);
        Assert.Equal(Expected.GetProperty("address").GetString(), danfe.Issuer.Address);
        Assert.Equal(Expected.GetProperty("discount_cents").GetInt64(), danfe.DiscountCents);
        Assert.Equal(Expected.GetProperty("change_cents").GetInt64(), danfe.ChangeCents);

        var items = Expected.GetProperty("items").EnumerateArray().Select(item => new DanfeItem(
            item.GetProperty("code").GetString()!, item.GetProperty("description").GetString()!,
            decimal.Parse(item.GetProperty("quantity").GetString()!, CultureInfo.InvariantCulture),
            item.GetProperty("unit").GetString()!, item.GetProperty("unit_price_cents").GetInt64(),
            item.GetProperty("total_cents").GetInt64())).ToList();
        Assert.Equal(items, danfe.Items);
        var payments = Expected.GetProperty("payments").EnumerateArray().Select(payment => new DanfePayment(
            payment.GetProperty("method").GetString()!, payment.GetProperty("amount_cents").GetInt64())).ToList();
        Assert.Equal(payments, danfe.Payments);
    }

    [Fact]
    public void The_payment_codes_are_the_inverse_of_what_the_fiscal_service_writes()
    {
        var written = Contract.GetProperty("payment_codes").EnumerateObject()
            .ToDictionary(entry => entry.Value.GetString()!, entry => entry.Name);

        Assert.Equal(written.OrderBy(e => e.Key), NfceProcReader.PaymentMethods.OrderBy(e => e.Key));
    }

    [Fact]
    public void Dates_are_printed_in_the_counter_time_zone()
    {
        var danfe = Read();

        var authorized = DateTimeOffset.Parse(Expected.GetProperty("authorized_at").GetString()!, CultureInfo.InvariantCulture);
        Assert.Equal(authorized.ToOffset(TimeSpan.FromHours(-3)).DateTime, danfe.AuthorizedLocal);
        var utc = NfceProcReader.Read(Xml, TimeZoneInfo.Utc);
        Assert.Equal(danfe.IssuedLocal.AddHours(3), utc.IssuedLocal);
    }

    [Fact]
    public void The_qr_code_is_printed_exactly_as_authorized()
    {
        var danfe = Read();
        var text = Encoding.Latin1.GetString(NfceDanfeLayout.Build(danfe, new PrinterLayout()));

        Assert.StartsWith("https://", danfe.QrCode);
        Assert.Contains(danfe.AccessKey, danfe.QrCode);
        Assert.Contains(danfe.QrCode, text);
        Assert.Contains(danfe.Protocol!, text);
    }

    [Theory]
    [InlineData("<cStat>100</cStat>", "<cStat>539</cStat>", "não está autorizada")]
    [InlineData("<tPag>17</tPag>", "<tPag>99</tPag>", "tPag 99")]
    [InlineData("<mod>65</mod>", "<mod>55</mod>", "modelo 65")]
    [InlineData("<tpEmis>1</tpEmis>", "<tpEmis>4</tpEmis>", "Tipo de emissão")]
    [InlineData("<vUnCom>59.0000000000</vUnCom>", "<vUnCom>59.0010000000</vUnCom>", "fração de centavo")]
    [InlineData("<vNF>60.00</vNF>", "<vNF>61.00</vNF>", "vNF")]
    [InlineData("<vTroco>5.00</vTroco>", "<vTroco>4.00</vTroco>", "não fecham")]
    public void An_incoherent_note_is_refused(string from, string to, string reason)
    {
        var error = Assert.Throws<DanfeException>(() => Read(Tamper(from, to)));

        Assert.Contains(reason, error.Message);
    }

    [Fact]
    public void A_protocol_of_another_note_is_refused()
    {
        var key = Expected.GetProperty("access_key").GetString()!;
        var other = key[..^1] + (key[^1] == '0' ? '1' : '0');

        var error = Assert.Throws<DanfeException>(() => Read(Tamper($"<chNFe>{key}</chNFe>", $"<chNFe>{other}</chNFe>")));

        Assert.Contains("outra nota", error.Message);
    }

    [Fact]
    public void A_signed_note_without_the_protocol_is_not_a_danfe()
    {
        var start = Xml.IndexOf("<protNFe", StringComparison.Ordinal);
        var end = Xml.IndexOf("</protNFe>", StringComparison.Ordinal) + "</protNFe>".Length;

        var error = Assert.Throws<DanfeException>(() => Read(Xml[..start] + Xml[end..]));

        Assert.Contains("protNFe", error.Message);
    }

    [Fact]
    public void A_dtd_is_never_processed()
    {
        var hostile = "<?xml version=\"1.0\"?><!DOCTYPE x [<!ENTITY e SYSTEM \"file:///etc/passwd\">]>" +
                      Xml[(Xml.IndexOf("?>", StringComparison.Ordinal) + 2)..].Replace("<xNome>", "<xNome>&e;", StringComparison.Ordinal);

        var error = Assert.Throws<DanfeException>(() => Read(hostile));

        Assert.Contains("ilegível", error.Message);
    }

    [Fact]
    public void An_oversized_answer_is_refused_before_parsing()
    {
        var error = Assert.Throws<DanfeException>(() => Read(Xml + new string(' ', NfceProcReader.MaxLength)));

        Assert.Contains("grande demais", error.Message);
    }

    [Fact]
    public void Nothing_to_read_is_said_plainly()
    {
        var error = Assert.Throws<DanfeException>(() => Read(""));

        Assert.Contains("não devolveu", error.Message);
    }
}
