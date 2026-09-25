using System.Text.Json;
using Pdv.App;
using Pdv.Core.Remote;
using Pdv.Core.Scale;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Remote;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>A tela do caixa diante dos pedidos do painel, sem janela (C5b-3).</summary>
public sealed class RemoteViewModelTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalProfile Terminal = new("tenant-1", "store-1", "device-1", "Pool Bar", true, "https://x/api");

    private static readonly JsonElement Pins =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.GetProperty("hashes")[0].Clone();

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly RemoteCommandService _remote;
    private readonly SaleViewModel _screen;
    private int _sequence;

    public RemoteViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        var hash = Pins.GetProperty("hash").GetString()!;
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) VALUES " +
            "('u-caixa', 'tenant-1', 'Ana Caixa', 'ana', 'cashier', $h, '0', 0, 1, 'x'), " +
            "('u-gerente', 'tenant-1', 'Bruno Gerente', 'bruno', 'manager', $h, '30', 1, 1, 'x')", ("$h", hash));

        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
        var auth = new StaffAuthentication(_database, Terminal.TenantId);
        _remote = new RemoteCommandService(_database, Terminal, Secret, ledger, auth);
        _journal = new SqliteTefJournal(_file.Path);
        _screen = new SaleViewModel(
            new ItemRegistration(_database, Terminal.Identity, ledger),
            new Catalog(_database.Connection, Terminal.TenantId),
            new Checkout(_database, Terminal.Identity, ledger, new TefCoordinator(new TefSimulator(), _journal)),
            new Identity("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m),
            adjustments: new SaleAdjustments(_database, Terminal.Identity, ledger),
            authorization: auth,
            stableWeight: () => new ToledoPrix3Protocol().Parse("00847"u8.ToArray(), DateTimeOffset.UtcNow),
            remote: _remote);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private RemoteCommand Issue(string kind, object payload)
    {
        var element = JsonSerializer.SerializeToElement(payload);
        var uuid = $"cmd-{++_sequence}";
        var at = DateTimeOffset.UtcNow.AddMinutes(-1).ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'");
        return new RemoteCommand(uuid, "tenant-1", "store-1", "device-1", kind, element, "u-gerente", "Bruno Gerente", at,
            CommandProtocol.Sign(Secret, uuid, "device-1", kind, element, at));
    }

    private void Sell()
    {
        _screen.AddCommand.Execute(new Catalog(_database.Connection, "tenant-1").Get("p-torta-kg")!);
        _screen.AddCommand.Execute(new Catalog(_database.Connection, "tenant-1").Get("p-refri")!);
    }

    [Fact]
    public void A_discount_from_the_panel_shows_up_on_the_open_sale()
    {
        Sell();
        _remote.Inbox.Accept(Issue(CommandProtocol.ApplyDiscount, new { order_id = _screen.OrderId, percent = 10, reason = "fiel" }));
        _remote.ApplyPending();

        Assert.Equal("R$ 41,04", _screen.Total);
        Assert.Equal("O painel alterou esta venda.", _screen.Notice);
    }

    [Fact]
    public void Another_sale_changed_by_the_panel_leaves_this_screen_alone()
    {
        Sell();
        _screen.ReloadIfOpen("outro-pedido");
        Assert.Null(_screen.Notice);
        Assert.Equal("R$ 45,60", _screen.Total);
    }

    private void CancelWhatTheKitchenHas()
    {
        Sell();
        var itemId = _screen.Lines[0].ItemId;
        _database.Execute(
            "INSERT INTO kds_tickets (id, tenant_id, store_id, order_id, order_item_id, product_name, status, queued_at, " +
            "created_at, updated_at, origin_device_id, client_uuid) " +
            "VALUES ('k-1', 'tenant-1', 'store-1', $order, $item, 'Mousse', 'ready', 'x', 'x', 'x', 'device-1', 'k')",
            ("$order", _screen.OrderId), ("$item", itemId));
        _remote.Inbox.Accept(Issue(CommandProtocol.CancelItem,
            new { order_id = _screen.OrderId, order_item_id = itemId, reason = "desistiu" }));
        _remote.ApplyPending();
        _screen.RefreshRemote();
    }

    [Fact]
    public async Task The_counter_sees_the_waiting_request_and_accepts_it_with_a_pin()
    {
        CancelWhatTheKitchenHas();
        Assert.Equal("1 pedido do painel espera o seu aceite.", _screen.RemoteNotice);

        RemoteDecisionRequest? asked = null;
        _screen.AskRemoteDecision = request =>
        {
            asked = request;
            return Task.FromResult<string?>(request.Accept("ana", Pins.GetProperty("pin").GetString()!));
        };
        await _screen.ReviewRemoteCommand.ExecuteAsync(null);

        Assert.Contains("pronto na cozinha", asked!.Note);
        Assert.Equal(["ana", "bruno"], asked.Logins);
        Assert.Equal(["Refrigerante lata"], _screen.Lines.Select(line => line.Name));
        Assert.Equal("item Mousse a granel cancelado com aceite de Ana Caixa no caixa", _screen.Notice);
        Assert.Null(_screen.RemoteNotice);
    }

    [Fact]
    public async Task Closing_the_dialog_decides_nothing()
    {
        CancelWhatTheKitchenHas();
        _screen.AskRemoteDecision = _ => Task.FromResult<string?>(null);

        await _screen.ReviewRemoteCommand.ExecuteAsync(null);

        Assert.Equal(1, _screen.RemoteAwaiting);
        Assert.Equal(2, _screen.Lines.Count);
    }
}
