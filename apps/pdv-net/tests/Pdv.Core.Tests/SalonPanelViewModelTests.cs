using System.Text;
using System.Text.Json.Nodes;
using Pdv.App;
using Pdv.Data;
using Pdv.Data.Edge;
using Pdv.Edge;

namespace Pdv.Core.Tests;

/// <summary>O painel do salão (F8) sem janela, sobre os serviços reais do salão.</summary>
public sealed class SalonPanelViewModelTests : IDisposable
{
    private static readonly JsonObject Contract =
        JsonNode.Parse(File.ReadAllText(TestDatabase.Contract("salon-http.json")))!.AsObject();

    private const string Joao = "5a1a0000-0000-4000-8000-0000000000a1";
    private const string Cafe = "5a1a0000-0000-4000-8000-0000000000b1";

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 26, 23, 0, 0, TimeSpan.Zero))
    {
        Zone = TimeZoneInfo.FindSystemTimeZoneById("America/Sao_Paulo"),
    };
    private readonly SalonServices _services;
    private readonly List<string> _questions = [];
    private bool _answer = true;

    public SalonPanelViewModelTests()
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

    private SalonPanelViewModel Panel(SalonEndpoint? endpoint = null)
    {
        var panel = new SalonPanelViewModel(_services.Auth, _services.Orders, _services.Kds, _services.Staff,
            endpoint ?? new SalonEndpoint("https", 8420, "192.168.0.10", "AB12 CD34 EF56 7890"), _clock);
        panel.Confirm = (title, question) =>
        {
            _questions.Add($"{title}: {question}");
            return Task.FromResult(_answer);
        };
        return panel;
    }

    private string PairWaiter(string name = "Celular da Ana")
    {
        var (code, _) = _services.Auth.CreatePairingCode();
        _services.Auth.Pair(code, name);
        return _services.Auth.ListDevices().Single(d => d.Name == name).Id;
    }

    private TableOrder OrderWithCoffee()
    {
        var table = _services.Tables.List()[0];
        var order = _services.Orders.OpenOrder(Guid.NewGuid().ToString(), Joao, "celular", table.Id);
        return _services.Orders.AddItem(order.Id, Guid.NewGuid().ToString(), Cafe, 2m);
    }

    [Fact]
    public void The_code_shows_in_two_groups_and_counts_down_until_it_expires()
    {
        var panel = Panel();
        Assert.Equal(SalonPanelViewModel.CodePlaceholder, panel.Code);
        Assert.False(panel.RevokeCodeCommand.CanExecute(null));

        panel.GenerateCodeCommand.Execute(null);
        Assert.Matches(@"^\d{4} \d{4}$", panel.Code);
        Assert.StartsWith("Vence em 5:00", panel.CodeHint);
        Assert.True(panel.RevokeCodeCommand.CanExecute(null));

        _clock.Advance(TimeSpan.FromSeconds(61));
        panel.Tick();
        Assert.StartsWith("Vence em 3:59", panel.CodeHint);

        // O código que está na tela é o que pareia.
        _services.Auth.Pair(panel.Code.Replace(" ", ""), "Celular");
        _clock.Advance(EdgeAuth.PairingTtl);
        panel.Tick();
        Assert.Equal(SalonPanelViewModel.CodePlaceholder, panel.Code);
        Assert.False(panel.CodeAlive);
        Assert.StartsWith("Gere um código", panel.CodeHint);
    }

    [Fact]
    public void A_revoked_code_leaves_the_screen_and_no_longer_pairs()
    {
        var panel = Panel();
        panel.GenerateCodeCommand.Execute(null);
        var code = panel.Code.Replace(" ", "");

        panel.RevokeCodeCommand.Execute(null);

        Assert.Equal(SalonPanelViewModel.CodePlaceholder, panel.Code);
        Assert.StartsWith("Código revogado", panel.CodeHint);
        Assert.Throws<PairingException>(() => _services.Auth.Pair(code, "Celular"));
        panel.Tick();
        Assert.StartsWith("Código revogado", panel.CodeHint);
    }

    [Fact]
    public void A_code_made_on_another_screen_is_announced_but_never_shown()
    {
        var panel = Panel();
        _services.Auth.CreatePairingCode();

        panel.Tick();

        // O texto só existe na tela que o gerou: aqui, só o aviso de que há um vivo.
        Assert.Equal(SalonPanelViewModel.CodePlaceholder, panel.Code);
        Assert.True(panel.CodeAlive);
        Assert.Contains("gerado em outra tela (5:00 restantes)", panel.CodeHint);
    }

    [Fact]
    public async Task Revoking_a_device_asks_first_and_then_locks_it_out()
    {
        var (code, _) = _services.Auth.CreatePairingCode();
        var token = _services.Auth.Pair(code, "Celular da Ana");
        var panel = Panel();
        Assert.Equal("nunca conectou", panel.Devices.Single().Status);

        // O contato é o pedido autenticado, na hora da loja (Brasília), não em UTC.
        _clock.Advance(TimeSpan.FromMinutes(7));
        var device = _services.Auth.Authenticate(token).Id;
        panel.Refresh();
        panel.SelectedDevice = panel.Devices.Single(d => d.Id == device);
        Assert.Equal(("Celular da Ana", "garçom", "visto 20:07:00"),
            (panel.SelectedDevice.Name, panel.SelectedDevice.Kind, panel.SelectedDevice.Status));

        _answer = false;
        await panel.RevokeDeviceCommand.ExecuteAsync(null);
        Assert.Single(_questions);
        Assert.Contains("Revogar Celular da Ana?", _questions[0]);
        Assert.Null(_services.Auth.ListDevices().Single().RevokedAt);

        _answer = true;
        await panel.RevokeDeviceCommand.ExecuteAsync(null);
        Assert.NotNull(_services.Auth.ListDevices().Single().RevokedAt);
        Assert.Equal("revogado", panel.SelectedDevice!.Status);
        Assert.False(panel.RevokeDeviceCommand.CanExecute(null));
    }

    [Fact]
    public async Task Ending_a_shift_drops_the_session_on_every_device()
    {
        var first = PairWaiter("Celular 1");
        var second = PairWaiter("Celular 2");
        _services.Staff.Login("joao", "4826", first);
        _services.Staff.Login("joao", "4826", second);
        var panel = Panel();
        Assert.Equal(2, panel.Staff.Count);
        Assert.All(panel.Staff, row => Assert.Equal(("João Garçom", "garçom", "visto 20:00:00"), (row.Name, row.Role, row.Seen)));
        Assert.False(panel.EndShiftCommand.CanExecute(null));

        panel.SelectedPerson = panel.Staff[0];
        await panel.EndShiftCommand.ExecuteAsync(null);

        Assert.Contains("Encerrar a sessão de João Garçom em todos os aparelhos?", _questions.Single());
        Assert.Empty(panel.Staff);
        Assert.Empty(_services.Staff.ListActive());
    }

    [Fact]
    public void The_open_tables_show_who_asked_for_the_bill()
    {
        var order = OrderWithCoffee();
        var panel = Panel();
        var row = Assert.Single(panel.Orders);
        Assert.Equal(("João", "", false), (row.Waiter, row.Status, row.BillRequested));
        Assert.Equal(order.LocalNumber.ToString("00000"), row.Number);
        Assert.Equal($"R$ {Pdv.Core.Printing.EscPosBuilder.FormatCents(order.TotalCents)}", row.Total);
        panel.SelectedOrder = row;

        _services.Orders.RequestBill(order.Id);
        panel.Refresh();

        Assert.Equal(("pedindo a conta", true), (panel.Orders[0].Status, panel.Orders[0].BillRequested));
        Assert.Equal(order.Id, panel.SelectedOrder?.Id);
    }

    [Fact]
    public void The_kitchen_queue_moves_forward_and_back_without_losing_the_selection()
    {
        OrderWithCoffee();
        var panel = Panel();
        var ticket = Assert.Single(panel.Tickets);
        Assert.Equal(("Café coado", "na fila", "00:00", false), (ticket.Item, ticket.Status, ticket.Wait, ticket.Late));
        Assert.False(panel.BumpCommand.CanExecute(null));

        panel.SelectedTicket = ticket;
        panel.BumpCommand.Execute(null);
        Assert.Equal(("preparando", ticket.Id), (panel.SelectedTicket?.Status, panel.SelectedTicket?.Id));

        panel.RecallCommand.Execute(null);
        Assert.Equal("na fila", panel.SelectedTicket?.Status);
        Assert.Equal("", panel.Error);

        // Na fila não há o que desfazer: o erro aparece e nada muda.
        panel.RecallCommand.Execute(null);
        Assert.Contains("não há o que desfazer", panel.Error);
        Assert.Equal("na fila", panel.SelectedTicket?.Status);

        _clock.Advance(TimeSpan.FromMinutes(KdsService.LateThresholdMinutes));
        panel.Refresh();
        Assert.True(panel.SelectedTicket?.Late);
        panel.BumpCommand.Execute(null);
        Assert.Equal("", panel.Error);
    }

    [Fact]
    public void A_delivered_ticket_leaves_the_queue()
    {
        OrderWithCoffee();
        var panel = Panel();
        panel.SelectedTicket = panel.Tickets[0];
        for (var i = 0; i < 3; i++) panel.BumpCommand.Execute(null);

        Assert.Empty(panel.Tickets);
        Assert.Null(panel.SelectedTicket);
        Assert.False(panel.BumpCommand.CanExecute(null));
    }

    [Fact]
    public void The_address_tells_what_to_type_on_the_phone_and_how_safe_it_is()
    {
        var secure = Panel();
        Assert.Equal(SalonAddressState.Secure, secure.AddressState);
        Assert.Contains("App do garçom: https://192.168.0.10:8420", secure.Address);
        Assert.Contains("Confira a digital: AB12 CD34 EF56 7890", secure.Address);

        var plain = Panel(new SalonEndpoint("http", 8420, "192.168.0.10", null));
        Assert.Equal(SalonAddressState.Plain, plain.AddressState);
        Assert.Contains("SEM CRIPTOGRAFIA", plain.Address);

        var off = new SalonPanelViewModel(_services.Auth, _services.Orders, _services.Kds, _services.Staff, null, _clock);
        Assert.Equal(SalonAddressState.Off, off.AddressState);
        Assert.Contains("DESLIGADO", off.Address);
    }

    [Theory]
    [InlineData(0, "00:00")]
    [InlineData(59, "00:59")]
    [InlineData(3599, "59:59")]
    [InlineData(3600, "1h00")]
    [InlineData(22643, "6h17")]
    [InlineData(-5, "00:00")]
    public void Waiting_time_reads_like_a_clock_until_an_hour(long seconds, string expected) =>
        Assert.Equal(expected, SalonPanelViewModel.Wait(seconds));

    [Theory]
    [InlineData(300, "5:00")]
    [InlineData(61, "1:01")]
    [InlineData(0, "0:00")]
    [InlineData(-1, "0:00")]
    public void The_countdown_reads_like_a_wall_clock(int seconds, string expected) =>
        Assert.Equal(expected, SalonPanelViewModel.Clock(seconds));
}
