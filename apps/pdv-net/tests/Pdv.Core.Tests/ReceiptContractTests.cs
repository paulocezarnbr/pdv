using System.Globalization;
using System.Text.Json;
using Pdv.Core.Printing;

namespace Pdv.Core.Tests;

/// <summary>O cupom ESC/POS contra <c>contracts/receipts.json</c>, gerado pelo Python: byte a byte.</summary>
public sealed class ReceiptContractTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("receipts.json"))).RootElement.Clone();

    private static string Hex(byte[] bytes) => Convert.ToHexString(bytes).ToLowerInvariant();

    private static readonly Dictionary<string, Func<EscPosBuilder>> Builders = new()
    {
        ["initialize"] = () => new EscPosBuilder().Initialize(),
        ["styles"] = () => new EscPosBuilder().AlignTo(1).Bold().Underline().Size(2, 3).Size(0, 9).ResetStyle(),
        ["text_pc850"] = () => new EscPosBuilder().Line("Ação ç ã é ü € — ☕ 🎂 fim"),
        ["columns_2_fit"] = () => new EscPosBuilder(20).Columns2("Subtotal", "12,50"),
        ["columns_2_truncates_left"] = () => new EscPosBuilder(20).Columns2("Um texto que não cabe", "1.234,56"),
        ["columns_2_value_bigger_than_line"] = () => new EscPosBuilder(5).Columns2("x", "123.456,78"),
        ["columns_3"] = () => new EscPosBuilder(24).Columns3("Descrição longa demais", "QTD", "TOTAL"),
        ["columns_3_falls_back"] = () => new EscPosBuilder(8).Columns3("abc", "QTD", "TOTAL"),
        ["feed_cut_drawer"] = () => new EscPosBuilder().Feed(3).Feed(999).Cut(4).Cut(0, partial: false).OpenDrawer()
            .OpenDrawer(pin: 5, onMs: 1, offMs: 9999),
        ["qrcode"] = () => new EscPosBuilder().QrCode("chave|3|1", moduleSize: 4),
        ["separator_centered"] = () => new EscPosBuilder(10).Separator('=').Centered("meio"),
    };

    [Fact]
    public void Every_builder_primitive_emits_the_pythons_bytes()
    {
        var cases = Contract.GetProperty("builder").EnumerateArray().ToList();
        Assert.Equal(Builders.Keys.Order(), cases.Select(c => c.GetProperty("name").GetString()!).Order());
        foreach (var expected in cases)
        {
            var name = expected.GetProperty("name").GetString()!;
            Assert.True(expected.GetProperty("hex").GetString() == Hex(Builders[name]().Build()), name);
        }
    }

    public static TheoryData<int> Receipts() => [.. Enumerable.Range(0, Contract.GetProperty("receipts").GetArrayLength())];

    [Theory]
    [MemberData(nameof(Receipts))]
    public void The_whole_receipt_is_the_pythons_byte_for_byte(int index)
    {
        var spec = Contract.GetProperty("receipts")[index];
        var items = spec.GetProperty("items").EnumerateArray().Select(item => new ReceiptItem(
            item.GetProperty("product_name").GetString()!, item.GetProperty("total_cents").GetInt64(),
            item.GetProperty("unit_price_cents").GetInt64(), item.GetProperty("quantity").GetString()!,
            item.GetProperty("net_weight_grams").GetInt64(), item.GetProperty("tare_grams").GetInt64())).ToList();
        var payments = spec.GetProperty("payments").EnumerateArray().Select(payment => new ReceiptPayment(
            payment.GetProperty("method").GetString()!, payment.GetProperty("amount_cents").GetInt64(),
            payment.GetProperty("change_cents").GetInt64())).ToList();
        var context = spec.GetProperty("context");
        string? Text(string name) => context.TryGetProperty(name, out var value) ? value.GetString() : null;
        long? Number(string name) => context.TryGetProperty(name, out var value) ? value.GetInt64() : null;

        var sale = new ReceiptSale(
            spec.GetProperty("local_number").GetInt64(), "0199-doc-uuid", items, spec.GetProperty("discount_cents").GetInt64(),
            DateTime.ParseExact(spec.GetProperty("printed_local").GetString()!, "yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
        var receipt = ReceiptLayout.BuildSaleReceipt(sale, payments, new ReceiptContext(
            Text("store_name")!, Text("store_document")!, Text("store_address")!, Text("operator_name")!, Text("terminal_label")!,
            Text("customer_name"), Number("cashback_earned_cents") ?? 0, Number("credit_balance_cents"),
            QrCodeData: Text("qrcode_data")), new PrinterLayout());

        Assert.Equal(spec.GetProperty("hex").GetString(), Hex(receipt));
    }

    [Fact]
    public void Drawer_pulse_and_formats_are_the_pythons()
    {
        Assert.Equal(Contract.GetProperty("drawer_pulse_hex").GetString(), Hex(ReceiptLayout.BuildDrawerPulse(new PrinterLayout())));
        foreach (var entry in Contract.GetProperty("format_cents").EnumerateObject())
        {
            Assert.Equal(entry.Value.GetString(), EscPosBuilder.FormatCents(long.Parse(entry.Name, CultureInfo.InvariantCulture)));
        }
        foreach (var entry in Contract.GetProperty("format_grams").EnumerateObject())
        {
            Assert.Equal(entry.Value.GetString(), EscPosBuilder.FormatGrams(long.Parse(entry.Name, CultureInfo.InvariantCulture)));
        }
    }
}
