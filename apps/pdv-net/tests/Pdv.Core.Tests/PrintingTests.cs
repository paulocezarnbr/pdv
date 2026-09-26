using System.Text;
using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Hardware;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>O cupom da venda gravada, a gravação em arquivo e a fila de impressão (C6c).</summary>
public sealed class PrintingTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalProfile Profile = new("tenant-1", "store-1", "device-1-abcdef", "Dolce Affetto", true, null);

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly AuditLedger _ledger;
    private readonly SqliteTefJournal _journal;
    private readonly string _folder = Path.Combine(Path.GetTempPath(), "pdv-cupons-" + Guid.NewGuid().ToString("N")[..8]);

    public PrintingTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) " +
            "VALUES ('u-gerente', 'tenant-1', 'Bruno', 'bruno', 'manager', NULL, '30', 1, 1, 'x')");
        _ledger = new AuditLedger(Profile.TenantId, Profile.StoreId, Profile.DeviceId, Secret);
        _journal = new SqliteTefJournal(_file.Path);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
        if (Directory.Exists(_folder)) Directory.Delete(_folder, recursive: true);
    }

    private static string Text(byte[] payload)
    {
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        return Encoding.GetEncoding(850).GetString(payload);
    }

    private async Task<string> SoldAndClosed(PaymentIntent payment, bool cancelOne = false)
    {
        var items = new ItemRegistration(_database, Profile.Identity, _ledger);
        var catalog = new Catalog(_database.Connection, "tenant-1");
        var first = items.RegisterUnitItem(null, catalog.Get("p-fatia")!, 1m, "u-caixa");
        var second = items.RegisterUnitItem(first.Order.Id, catalog.Get("p-refri")!, 1.5m, "u-caixa");
        if (cancelOne)
        {
            new SaleAdjustments(_database, Profile.Identity, _ledger).CancelItem(
                first.Order.Id, second.Item.Id, "u-caixa", new Identity("u-gerente", "Bruno", "bruno", "manager", true, 30m), "trocou");
        }
        var total = Convert.ToInt64(_database.Scalar($"SELECT total_cents FROM orders WHERE id = '{first.Order.Id}'"));
        await new Checkout(_database, Profile.Identity, _ledger, new TefCoordinator(new TefSimulator(), _journal))
            .CloseAsync(first.Order.Id, "u-caixa", [payment with { AmountCents = payment.IsCard ? total : payment.AmountCents }], new Quiet());
        return first.Order.Id;
    }

    private ReceiptComposer Composer() => new(_database, Profile, new PrinterSettings(OutputDirectory: _folder));

    [Fact]
    public async Task The_receipt_says_what_was_recorded_and_the_drawer_opens_for_cash()
    {
        var orderId = await SoldAndClosed(PaymentIntent.Cash(2000), cancelOne: true);

        var payload = Composer().Compose(orderId, "Ana Caixa");
        var text = Text(payload);

        Assert.Contains("Fatia de torta", text);
        Assert.DoesNotContain("Refrigerante", text); // cancelado não sai no papel
        Assert.Contains("TROCO", text);
        Assert.Contains("Doc: " + _database.Scalar($"SELECT client_uuid FROM orders WHERE id = '{orderId}'"), text);
        Assert.Contains("PDV device-1", text);
        Assert.EndsWith(Convert.ToHexString([0x1B, 0x70, 0x00, 12, 125]), Convert.ToHexString(payload));
    }

    [Fact]
    public async Task A_card_sale_never_opens_the_drawer_and_shows_the_quantity_as_recorded()
    {
        var orderId = await SoldAndClosed(PaymentIntent.Card(TefCardType.Debit, 0));
        var payload = Composer().Compose(orderId, "Ana", customerName: "Lia", cashbackEarnedCents: 73, prepaidBalanceCents: 0);
        var text = Text(payload);

        Assert.True(payload.AsSpan().IndexOf([(byte)0x1B, (byte)0x70]) < 0, "ESC p num cupom sem dinheiro");
        Assert.Contains("   1.5 x 3,33", text);
        Assert.Contains("Cashback creditado", text);
        Assert.Contains("Saldo pre-pago", text);
    }

    [Fact]
    public void The_file_printer_keeps_the_bytes_and_a_readable_preview()
    {
        var payload = new Pdv.Core.Printing.EscPosBuilder().Initialize().Line("Ação").Cut().Build();
        new FilePrinter(_folder).Send(payload);

        Assert.Equal(payload, File.ReadAllBytes(Directory.GetFiles(_folder, "*.bin").Single()));
        Assert.Contains("Ação", File.ReadAllText(Directory.GetFiles(_folder, "*.txt").Single()));
    }

    [Fact]
    public void The_settings_come_from_what_detection_wrote()
    {
        Assert.Equal("file", PrinterSettings.Load(_database, _folder).Backend);
        _database.Execute(
            "INSERT INTO device_settings (key, value, updated_at) VALUES ('printer.backend', 'win32raw', 'x'), " +
            "('printer.name', 'EPSON TM-T20X', 'x'), ('store.document', '12.345.678/0001-90', 'x')");
        var settings = PrinterSettings.Load(_database, _folder);
        Assert.Equal(("win32raw", "EPSON TM-T20X", "12.345.678/0001-90"), (settings.Backend, settings.WindowsPrinterName, settings.StoreDocument));
        Assert.IsType<Win32RawPrinter>(settings.Build());
    }

    private sealed class FlakyPrinter(int failures) : IPrinter
    {
        public int Calls;
        public List<byte[]> Delivered { get; } = [];

        public void Send(byte[] payload, string jobName = "PDV Cupom")
        {
            if (Interlocked.Increment(ref Calls) <= failures) throw new PrinterException("sem papel");
            Delivered.Add(payload);
        }
    }

    [Fact]
    public void The_queue_retries_and_a_persistent_failure_is_reported_not_thrown()
    {
        var flaky = new FlakyPrinter(failures: 2);
        using (var service = new PrintService(flaky, backoff: TimeSpan.FromMilliseconds(1)))
        {
            service.Submit([1, 2, 3]);
        }
        Assert.Equal([1, 2, 3], flaky.Delivered.Single());

        var dead = new FlakyPrinter(failures: int.MaxValue);
        var errors = new List<string>();
        using (var service = new PrintService(dead, backoff: TimeSpan.FromMilliseconds(1)))
        {
            service.Failed += errors.Add;
            service.Submit([9]);
        }
        Assert.Equal(3, dead.Calls);
        Assert.Contains("após 3 tentativas: sem papel", errors.Single());
    }

    [Fact]
    public async Task Closing_on_the_screen_hands_the_sale_to_the_receipt()
    {
        var screen = new SaleViewModel(
            new ItemRegistration(_database, Profile.Identity, _ledger),
            new Catalog(_database.Connection, "tenant-1"),
            new Checkout(_database, Profile.Identity, _ledger, new TefCoordinator(new TefSimulator(), _journal)),
            new Identity("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m));
        ClosedSale? closed = null;
        screen.SaleClosed += (_, sale) => closed = sale;

        screen.AddCommand.Execute(new Catalog(_database.Connection, "tenant-1").Get("p-fatia")!);
        var orderId = screen.OrderId;
        await screen.PayCashCommand.ExecuteAsync(null);

        Assert.Equal((orderId, "Ana Caixa"), (closed!.OrderId, closed.OperatorName));
        Assert.Contains("Fatia de torta", Text(Composer().Compose(closed.OrderId, closed.OperatorName)));
    }

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
