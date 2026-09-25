using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>
/// A venda no balcão com cartão, de ponta a ponta sobre o banco real: o que
/// ficou gravado, o que sobe para a nuvem, e onde terminou o dinheiro do cliente.
/// </summary>
public sealed class CheckoutTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly TefSimulator _tef = new();
    private readonly AuditLedger _ledger = new(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);

    public CheckoutTests()
    {
        _database = new PdvDatabase(_file.Path);
        _journal = new SqliteTefJournal(_file.Path);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private TefCoordinator Coordinator() => new(_tef, _journal);

    private Checkout Checkout() => new(_database, Terminal, _ledger, Coordinator());

    private string OrderOf(long totalCents)
    {
        return _database.InTransaction(tx =>
        {
            var order = new SaleRepository(Terminal).CreateOrder(tx, "user-1");
            using (var item = tx.Command(
                       """
                       INSERT INTO order_items (id, order_id, tenant_id, product_id, product_name, pricing_mode,
                                                unit_price_cents, total_cents, created_at, client_uuid)
                       VALUES ($id, $order, 'tenant-1', 'p-1', 'Bolo de pote', 'unit', $total, $total,
                               '2026-09-25T10:00:00.000+00:00', $uuid)
                       """,
                       ("$id", Iso.NewId()), ("$order", order.Id), ("$total", totalCents), ("$uuid", Iso.NewId())))
            {
                item.ExecuteNonQuery();
            }
            using (var totals = tx.Command(
                       "UPDATE orders SET subtotal_cents = $t, total_cents = $t WHERE id = $id",
                       ("$t", totalCents), ("$id", order.Id)))
            {
                totals.ExecuteNonQuery();
            }
            return order.Id;
        });
    }

    private string Status(string orderId) => (string)_database.Scalar("SELECT status FROM orders WHERE id = $id", ("$id", orderId))!;

    private long Count(string sql) => Convert.ToInt64(_database.Scalar(sql));

    private List<(string Table, JsonElement Payload)> OutboxRows() =>
        Query("SELECT entity_table, payload_json FROM sync_outbox ORDER BY seq")
            .Select(row => (row[0], JsonDocument.Parse(row[1]).RootElement.Clone())).ToList();

    private List<string[]> Query(string sql)
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = sql;
        using var reader = command.ExecuteReader();
        var rows = new List<string[]>();
        while (reader.Read())
        {
            rows.Add(Enumerable.Range(0, reader.FieldCount).Select(i => reader.IsDBNull(i) ? "" : reader.GetValue(i).ToString()!).ToArray());
        }
        return rows;
    }

    private static readonly SilentInteraction Ui = new();

    // -- o caminho feliz ----------------------------------------------------

    [Fact]
    public async Task A_card_sale_is_saved_uploaded_and_confirmed()
    {
        var order = OrderOf(4500);
        var result = await Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 4500)], Ui);

        var closed = Assert.IsType<CheckoutResult.Closed>(result);
        Assert.True(closed.AllConfirmed);
        var approval = Assert.Single(closed.Approvals);
        Assert.Equal("paid", Status(order));
        Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]);

        var payment = Assert.Single(Query("SELECT method, amount_cents, nsu, client_uuid FROM payments"));
        Assert.Equal(["debit", "4500", approval.Nsu, approval.TransactionId], payment);

        _ledger.Verify(_database.Connection);
        Assert.Equal(["orders", "payments", "audit_ledger"], OutboxRows().Select(row => row.Table));
    }

    [Fact]
    public async Task The_order_goes_up_with_the_keys_of_the_push_contract()
    {
        var order = OrderOf(4500);
        await Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Credit, 4500, 3)], Ui);

        var contract = JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("push-day.json"))).RootElement;
        var counterSale = contract.GetProperty("items").EnumerateArray().First(item =>
            item.GetProperty("entity_table").GetString() == "orders" &&
            item.GetProperty("operation").GetString() == "insert" &&
            item.GetProperty("payload").TryGetProperty("status", out var status) && status.GetString() == "paid");
        var contractPayment = contract.GetProperty("items").EnumerateArray()
            .First(item => item.GetProperty("entity_table").GetString() == "payments");

        var rows = OutboxRows();
        Assert.Equal(Keys(counterSale.GetProperty("payload")), Keys(rows.Single(r => r.Table == "orders").Payload));
        // O cartão leva o NSU a mais — a nuvem já aceita (payments.nsu, 001_init).
        Assert.Equal(
            Keys(contractPayment.GetProperty("payload")).Append("nsu").Order(),
            Keys(rows.Single(r => r.Table == "payments").Payload));
    }

    private static IEnumerable<string> Keys(JsonElement element) =>
        element.EnumerateObject().Select(property => property.Name).Order();

    [Fact]
    public async Task Cash_and_card_split_gives_change_only_in_cash()
    {
        var order = OrderOf(5000);
        var result = await Checkout().CloseAsync(order, "user-1",
            [PaymentIntent.Card(TefCardType.Debit, 3000), PaymentIntent.Cash(2500)], Ui);

        var closed = Assert.IsType<CheckoutResult.Closed>(result);
        Assert.Equal([0L, 500L], closed.Payments.Select(p => p.ChangeCents));
        Assert.Equal("paid", Status(order));
    }

    [Fact]
    public async Task Two_cards_on_one_sale()
    {
        var order = OrderOf(9000);
        var result = await Checkout().CloseAsync(order, "user-1",
            [PaymentIntent.Card(TefCardType.Credit, 6000), PaymentIntent.Card(TefCardType.Debit, 3000)], Ui);

        var closed = Assert.IsType<CheckoutResult.Closed>(result);
        Assert.All(closed.Approvals, approval => Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]));
    }

    // -- o que não pode acontecer ------------------------------------------

    [Fact]
    public async Task A_split_that_does_not_add_up_reads_no_card()
    {
        var order = OrderOf(1000);
        await Assert.ThrowsAsync<InsufficientPaymentException>(() =>
            Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 500)], Ui));
        Assert.Empty(_tef.Host);
        Assert.Equal("open", Status(order));
    }

    [Fact]
    public async Task Card_overpayment_is_never_returned_as_cash()
    {
        // R$ 15 no cartão + R$ 5 em dinheiro numa venda de R$ 10: o Python
        // devolvia R$ 10 em espécie. Aqui a venda nem chega ao cartão.
        var order = OrderOf(1000);
        var error = await Assert.ThrowsAsync<InsufficientPaymentException>(() =>
            Checkout().CloseAsync(order, "user-1",
                [PaymentIntent.Card(TefCardType.Credit, 1500), PaymentIntent.Cash(500)], Ui));
        Assert.Contains("não há troco para cartão", error.Message);
        Assert.Empty(_tef.Host);
    }

    [Fact]
    public async Task A_declined_card_leaves_the_sale_open_and_nothing_charged()
    {
        var order = OrderOf(1051);
        var result = await Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 1051)], Ui);

        Assert.Contains("Saldo insuficiente", Assert.IsType<CheckoutResult.CardRefused>(result).Reason);
        Assert.Equal("open", Status(order));
        Assert.Equal(0, Count("SELECT COUNT(*) FROM payments"));
        Assert.Equal(0, Count("SELECT COUNT(*) FROM sync_outbox"));
    }

    [Fact]
    public async Task When_the_second_card_is_declined_the_first_is_undone()
    {
        var order = OrderOf(4051);
        var result = await Checkout().CloseAsync(order, "user-1",
            [PaymentIntent.Card(TefCardType.Credit, 3000), PaymentIntent.Card(TefCardType.Debit, 1051)], Ui);

        Assert.IsType<CheckoutResult.CardRefused>(result);
        var first = Assert.Single(_tef.Host);
        Assert.Equal(HostState.Undone, first.Value);
        Assert.Equal("open", Status(order));
        Assert.Empty(_journal.Pending());
    }

    [Fact]
    public async Task A_sale_that_fails_to_save_undoes_the_card()
    {
        var order = OrderOf(2000);
        // Disco cheio, banco travado, trigger — qualquer coisa que impeça gravar.
        _database.Execute(
            "CREATE TEMP TRIGGER quebra BEFORE INSERT ON payments BEGIN SELECT RAISE(ABORT, 'disco cheio'); END");

        await Assert.ThrowsAsync<SqliteException>(() =>
            Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 2000)], Ui));

        Assert.Equal(HostState.Undone, Assert.Single(_tef.Host).Value);
        Assert.Equal("open", Status(order));
        Assert.Equal(0, Count("SELECT COUNT(*) FROM sync_outbox"));
        Assert.Empty(_journal.Pending());
    }

    // -- queda no meio ------------------------------------------------------

    [Fact]
    public async Task Crash_after_saving_confirms_at_the_next_opening()
    {
        var order = OrderOf(3000);
        _tef.Offline = false;
        var checkout = new Checkout(_database, Terminal, _ledger, new TefCoordinator(new OfflineOnConfirm(_tef), _journal));
        var closed = Assert.IsType<CheckoutResult.Closed>(
            await checkout.CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 3000)], Ui));
        Assert.False(closed.AllConfirmed);
        var approval = Assert.Single(closed.Approvals);
        Assert.Equal(HostState.Authorized, _tef.Host[approval.TransactionId]);

        // reabertura do caixa
        var recovered = Assert.Single(await Coordinator().RecoverPendingAsync(
            entry => SaleRepository.WasRecorded(_database.Connection, entry.TransactionId)));
        Assert.Equal(TefState.Confirmed, recovered.Resolution);
        Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]);
    }

    [Fact]
    public async Task Crash_before_saving_undoes_at_the_next_opening()
    {
        var order = OrderOf(3000);
        // O cartão foi aprovado e o caixa caiu antes de gravar a venda.
        var outcome = await Coordinator().AuthorizeAsync(order, 3000, TefCardType.Debit, Ui);
        var approval = Assert.IsType<TefOutcome.Approved>(outcome).Approval;

        var recovered = Assert.Single(await Coordinator().RecoverPendingAsync(
            entry => SaleRepository.WasRecorded(_database.Connection, entry.TransactionId)));
        Assert.Equal(TefState.Undone, recovered.Resolution);
        Assert.Equal(HostState.Undone, _tef.Host[approval.TransactionId]);
        Assert.Equal("open", Status(order));
    }

    [Fact]
    public async Task A_closed_order_cannot_be_paid_twice()
    {
        var order = OrderOf(1000);
        await Checkout().CloseAsync(order, "user-1", [PaymentIntent.Cash(1000)], Ui);
        await Assert.ThrowsAsync<OrderNotOpenException>(() =>
            Checkout().CloseAsync(order, "user-1", [PaymentIntent.Card(TefCardType.Debit, 1000)], Ui));
        Assert.Empty(_tef.Host);
    }

    private sealed class SilentInteraction : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }

    /// <summary>A rede cai exatamente na hora de confirmar.</summary>
    private sealed class OfflineOnConfirm(TefSimulator inner) : ITefProvider
    {
        public string Name => inner.Name;

        public Task<TefOutcome> AuthorizeAsync(TefRequest request, ITefInteraction ui, CancellationToken cancellationToken) =>
            inner.AuthorizeAsync(request, ui, cancellationToken);

        public Task ConfirmAsync(TefReference reference, CancellationToken cancellationToken) =>
            throw new IOException("Sem comunicação com o TEF");

        public Task UndoAsync(TefReference reference, CancellationToken cancellationToken) =>
            inner.UndoAsync(reference, cancellationToken);
    }
}
