using System.Text;
using System.Text.Json.Nodes;
using Pdv.App;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Edge;
using Pdv.Data.Sales;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>As mesas do caixa (F9) sem janela, sobre os serviços reais do salão.</summary>
public sealed class TablesViewModelTests : IDisposable
{
    private static readonly JsonObject Contract =
        JsonNode.Parse(File.ReadAllText(TestDatabase.Contract("salon-http.json")))!.AsObject();

    private const string Joao = "5a1a0000-0000-4000-8000-0000000000a1";
    private const string Cafe = "5a1a0000-0000-4000-8000-0000000000b1";
    private const string Fatia = "5a1a0000-0000-4000-8000-0000000000b3";

    private static readonly Identity Cashier = new("caixa-1", "Ana Caixa", "ana", "cashier", false, 0);

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 26, 23, 0, 0, TimeSpan.Zero));
    private readonly SalonServices _services;
    private readonly List<string> _log = [];
    private readonly List<SettledOrder> _settled = [];
    private bool _confirm = true;
    private long? _tip = 0;
    private Func<long, IReadOnlyList<PaymentIntent>?> _pay = charged => [PaymentIntent.Cash(charged)];
    private Func<ItemPickRequest, IReadOnlyList<string>?> _pick = _ => null;
    private int? _target = 0;
    private TipRequest? _tipAsked;
    private long? _charged;

    public TablesViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        SalonScript.Seed(_database, Contract);
        var profile = new TerminalProfile(
            Contract["tenant_id"]!.GetValue<string>(), Contract["store_id"]!.GetValue<string>(),
            Contract["device_id"]!.GetValue<string>(), "Confeitaria Aurora", true, null);
        var ledger = new AuditLedger(profile.TenantId, profile.StoreId, profile.DeviceId, Encoding.UTF8.GetBytes("chave"));
        _services = new SalonServices(_database, profile, ledger, new EventHub(), clock: _clock);
        _services.Tables.SeedDefaultTables(4);
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private TablesViewModel Screen()
    {
        var screen = new TablesViewModel(_services.Orders, _services.Tables, Cashier, _clock)
        {
            Confirm = (title, question) =>
            {
                _log.Add($"confirmar {title}: {question}");
                return Task.FromResult(_confirm);
            },
            Inform = (title, message) =>
            {
                _log.Add($"{title}: {message}");
                return Task.CompletedTask;
            },
            AskTip = request =>
            {
                _tipAsked = request;
                return Task.FromResult(_tip);
            },
            AskPayments = charged =>
            {
                _charged = charged;
                return Task.FromResult(_pay(charged));
            },
            PickItems = request => Task.FromResult(_pick(request)),
            PickTarget = (_, _) => Task.FromResult(_target),
        };
        screen.Settled += (_, settled) => _settled.Add(settled);
        return screen;
    }

    private TableOrder Open(int table, params (string Product, decimal Quantity)[] items)
    {
        var order = _services.Orders.OpenOrder(Guid.NewGuid().ToString(), Joao, "celular", _services.Tables.List()[table].Id);
        foreach (var (product, quantity) in items)
        {
            order = _services.Orders.AddItem(order.Id, Guid.NewGuid().ToString(), product, quantity);
        }
        return order;
    }

    private string Status(string orderId) =>
        (string)_database.Scalar("SELECT status FROM orders WHERE id = $id", ("$id", orderId))!;

    [Fact]
    public void Lists_every_open_table_with_area_waiter_and_time()
    {
        var order = Open(0, (Cafe, 2m));
        _clock.Advance(TimeSpan.FromMinutes(7));
        var screen = Screen();

        var row = Assert.Single(screen.Rows);
        Assert.Equal(("Salão", "ocupada", "João Garçom", "1", "07min"), (row.Area, row.Status, row.Waiter, row.Items, row.Elapsed));
        Assert.Equal(order.LocalNumber.ToString("00000"), row.Number);
        Assert.Equal(Money.Format(1400), row.Total);
        Assert.Equal($"1 de 1 comandas abertas · 0 pedindo a conta · 3 mesas livres · na tela: {Money.Format(1400)}", screen.Summary);
    }

    [Fact]
    public void Search_covers_area_waiter_and_number_and_filters_who_asked_for_the_bill()
    {
        var first = Open(0, (Cafe, 1m));
        var second = Open(1, (Fatia, 1m));
        _services.Orders.RequestBill(second.Id);
        var screen = Screen();

        screen.Query = "joão salão";
        Assert.Equal(2, screen.Rows.Count);
        screen.Query = second.LocalNumber.ToString("00000");
        Assert.Equal(second.Id, Assert.Single(screen.Rows).Id);
        screen.Query = "varanda";
        Assert.Empty(screen.Rows);
        Assert.StartsWith("0 de 2 comandas abertas · 1 pedindo a conta", screen.Summary);

        screen.Query = "";
        screen.OnlyBilling = true;
        var row = Assert.Single(screen.Rows);
        Assert.Equal((second.Id, "pedindo a conta", true), (row.Id, row.Status, row.BillRequested));
        Assert.DoesNotContain(screen.Rows, r => r.Id == first.Id);
    }

    [Fact]
    public async Task Receiving_a_table_that_did_not_ask_needs_a_second_gesture()
    {
        var order = Open(0, (Cafe, 2m));
        var screen = Screen();
        screen.Selected = screen.Rows[0];

        _confirm = false;
        await screen.ReceiveCommand.ExecuteAsync(null);
        Assert.Contains("ainda não pediu a conta", Assert.Single(_log));
        Assert.Null(_tipAsked);
        Assert.Equal("open", Status(order.Id));

        _confirm = true;
        await screen.ReceiveCommand.ExecuteAsync(null);
        Assert.Equal("paid", Status(order.Id));
    }

    [Fact]
    public async Task Receives_the_bill_with_tip_and_change_and_frees_the_table()
    {
        var order = Open(0, (Cafe, 2m), (Fatia, 1m));
        _services.Orders.RequestBill(order.Id);
        var screen = Screen();
        screen.Selected = screen.Rows[0];
        _tip = 286;
        _pay = _ => [PaymentIntent.Cash(5000)];

        await screen.ReceiveCommand.ExecuteAsync(null);

        // Pediu a conta: nada de confirmação. A gorjeta sugerida é 10% para baixo.
        Assert.DoesNotContain(_log, line => line.StartsWith("confirmar"));
        Assert.Equal(2850, _tipAsked!.TotalCents);
        Assert.Equal(285, _tipAsked.SuggestedCents);
        Assert.Contains("atendida por João Garçom", _tipAsked.Details);
        Assert.Equal(3136, _charged);
        Assert.Equal("paid", Status(order.Id));
        Assert.Equal(286L, _database.Scalar("SELECT tip_cents FROM orders WHERE id = $id", ("$id", order.Id)));
        Assert.Equal(order.Id, Assert.Single(_settled).Order.Id);
        Assert.Equal($"Conta recebida: Mesa 1 recebida — {Money.Format(3136)} · gorjeta {Money.Format(286)} para João · " +
            $"troco {Money.Format(1864)}", _log.Last());
        Assert.Empty(screen.Rows);
        Assert.Null(screen.Selected);
    }

    [Fact]
    public async Task Giving_up_at_the_tip_or_at_the_payment_leaves_the_table_open()
    {
        var order = Open(0, (Cafe, 1m));
        _services.Orders.RequestBill(order.Id);
        var screen = Screen();
        screen.Selected = screen.Rows[0];

        _tip = null;
        await screen.ReceiveCommand.ExecuteAsync(null);
        Assert.Null(_charged);

        _tip = 0;
        _pay = _ => null;
        await screen.ReceiveCommand.ExecuteAsync(null);
        Assert.Equal(700, _charged);
        Assert.Equal("open", Status(order.Id));
        Assert.Empty(_settled);
    }

    [Fact]
    public async Task A_short_payment_is_refused_with_the_reason_and_nothing_closes()
    {
        var order = Open(0, (Fatia, 1m));
        _services.Orders.RequestBill(order.Id);
        var screen = Screen();
        screen.Selected = screen.Rows[0];
        _pay = _ => [new PaymentIntent(PaymentMethods.Debit, 1000)];

        await screen.ReceiveCommand.ExecuteAsync(null);

        Assert.Contains("Faltam", screen.Error);
        Assert.Equal("open", Status(order.Id));
        Assert.Empty(_settled);
        Assert.Equal(order.Id, screen.Selected?.Id);
    }

    [Fact]
    public async Task Paying_part_charges_only_the_chosen_items_and_the_rest_stays_open()
    {
        var order = Open(0, (Cafe, 1m), (Fatia, 2m));
        var screen = Screen();
        screen.Selected = screen.Rows[0];
        ItemPickRequest? offered = null;
        _pick = request =>
        {
            offered = request;
            return [request.Items.Single(item => item.Name == "Café coado").Id];
        };
        _tip = 70;

        await screen.ReceivePartCommand.ExecuteAsync(null);

        Assert.Equal(("Mesa 1 — o que este cliente vai pagar", "Ir para a gorjeta", 2), (offered!.Title, offered.Confirm, offered.Items.Count));
        // A gorjeta enxerga só a parte: 10% de R$ 7,00, não da mesa inteira.
        Assert.Equal((700L, 70L), (_tipAsked!.TotalCents, _tipAsked.SuggestedCents));
        Assert.Equal(770, _charged);
        Assert.Equal("open", Status(order.Id));
        Assert.Equal(2900, _services.Orders.GetOrder(order.Id).TotalCents);
        Assert.NotEqual(order.Id, Assert.Single(_settled).Order.Id);
        Assert.Equal(Money.Format(2900), Assert.Single(screen.Rows).Total);
    }

    [Fact]
    public async Task A_canceled_item_is_never_offered()
    {
        var order = Open(0, (Cafe, 1m), (Fatia, 1m));
        _database.Execute("UPDATE order_items SET canceled_at = 'x' WHERE order_id = $id AND product_name = 'Café coado'",
            ("$id", order.Id));
        var screen = Screen();
        screen.Selected = screen.Rows[0];
        ItemPickRequest? offered = null;
        _pick = request =>
        {
            offered = request;
            return null;
        };

        await screen.ReceivePartCommand.ExecuteAsync(null);

        Assert.Equal("Fatia de torta", Assert.Single(offered!.Items).Name);
        Assert.Null(_tipAsked);
    }

    [Fact]
    public async Task Moves_items_to_another_table()
    {
        var source = Open(0, (Cafe, 1m), (Fatia, 1m));
        var target = Open(1, (Cafe, 1m));
        var screen = Screen();
        screen.Selected = screen.Rows.Single(r => r.Id == source.Id);
        _pick = request => [request.Items.Single(item => item.Name == "Fatia de torta").Id];

        await screen.MoveItemsCommand.ExecuteAsync(null);

        Assert.Equal("", screen.Error);
        Assert.Equal(700, _services.Orders.GetOrder(source.Id).TotalCents);
        Assert.Equal(2150, _services.Orders.GetOrder(target.Id).TotalCents);
        Assert.Equal(source.Id, screen.Selected?.Id);
    }

    [Fact]
    public async Task Merging_asks_with_the_new_total_and_frees_the_source_table()
    {
        var source = Open(0, (Cafe, 1m));
        var target = Open(1, (Fatia, 1m));
        var screen = Screen();
        screen.Selected = screen.Rows.Single(r => r.Id == source.Id);

        _confirm = false;
        await screen.MergeCommand.ExecuteAsync(null);
        Assert.Contains($"fica em {Money.Format(2150)} e a Mesa 1 é liberada", Assert.Single(_log));
        Assert.Equal("open", Status(source.Id));

        _confirm = true;
        await screen.MergeCommand.ExecuteAsync(null);
        Assert.Equal("canceled", Status(source.Id));
        Assert.Equal(target.Id, Assert.Single(screen.Rows).Id);
        Assert.Equal(2150, _services.Orders.GetOrder(target.Id).TotalCents);
    }

    [Fact]
    public async Task Without_a_selection_or_a_destination_it_says_so()
    {
        Open(0, (Cafe, 1m));
        var screen = Screen();

        await screen.ReceiveCommand.ExecuteAsync(null);
        await screen.MergeCommand.ExecuteAsync(null);
        screen.Selected = screen.Rows[0];
        await screen.MoveItemsCommand.ExecuteAsync(null);

        Assert.Equal(
            ["Receber: Selecione uma mesa na lista.", "Juntar comandas: Selecione uma mesa na lista.",
             "Sem destino: Não há outra comanda aberta no salão."], _log);
    }

    [Fact]
    public void The_selection_survives_the_refresh_even_when_the_row_changes()
    {
        var order = Open(0, (Cafe, 1m));
        Open(1, (Fatia, 1m));
        var screen = Screen();
        screen.Selected = screen.Rows.Single(r => r.Id == order.Id);

        _services.Orders.RequestBill(order.Id);
        screen.Refresh();

        Assert.Equal((order.Id, "pedindo a conta"), (screen.Selected?.Id, screen.Selected?.Status));
    }

    [Fact]
    public async Task A_table_closed_elsewhere_is_refused_not_crashed()
    {
        var order = Open(0, (Cafe, 1m));
        _services.Orders.RequestBill(order.Id);
        var screen = Screen();
        screen.Selected = screen.Rows[0];
        _services.Orders.Settle(order.Id, [PaymentIntent.Cash(700)], "outro", "Outro caixa");

        await screen.ReceiveCommand.ExecuteAsync(null);

        Assert.NotEqual("", screen.Error);
        Assert.Empty(_settled);
        Assert.Empty(screen.Rows);
    }

    [Theory]
    [InlineData(1999, 199)]
    [InlineData(2850, 285)]
    [InlineData(9, 0)]
    public void The_suggested_tip_rounds_down_to_the_cent(long total, long tip) =>
        Assert.Equal(tip, TablesViewModel.SuggestedTip(total));

    [Theory]
    [InlineData(null, "—")]
    [InlineData("lixo", "—")]
    [InlineData("2026-09-26T22:53:00+00:00", "07min")]
    [InlineData("2026-09-26T17:48:00+00:00", "5h12")]
    [InlineData("2026-09-26T23:05:00+00:00", "00min")]
    public void Elapsed_time_reads_minutes_then_hours(string? openedAt, string expected) =>
        Assert.Equal(expected, TablesViewModel.Elapsed(openedAt, new DateTimeOffset(2026, 9, 26, 23, 0, 0, TimeSpan.Zero)));
}
