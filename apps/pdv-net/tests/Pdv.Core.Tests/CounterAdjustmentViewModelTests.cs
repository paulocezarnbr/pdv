using System.Text.Json;
using Pdv.App;
using Pdv.Core.Scale;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>A tela de venda com balança, F4 e F6, sem janela, sobre o banco real (C3c).</summary>
public sealed class CounterAdjustmentViewModelTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private static readonly JsonElement First =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.GetProperty("hashes")[0].Clone();

    private static readonly string Pin = First.GetProperty("pin").GetString()!;

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly SaleViewModel _screen;
    private readonly Queue<string?> _answers = new();
    private readonly List<AuthorizationRequest> _requests = [];
    private ScaleReading? _plate;

    /// <summary>O que o "gerente" digita no diálogo; o diálogo desiste depois de um erro.</summary>
    private (string Login, string Pin)? _credentials = ("bruno", Pin);

    private string? _dialogError;

    public CounterAdjustmentViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        var hash = First.GetProperty("hash").GetString()!;
        foreach (var (id, name, login, role, auth, discount) in new[]
                 {
                     ("u-caixa", "Ana Caixa", "ana", "cashier", 0, "0"),
                     ("u-gerente", "Bruno Gerente", "bruno", "manager", 1, "30"),
                     ("u-dona", "Carla Dona", "carla", "owner", 1, "100"),
                 })
        {
            _database.Execute(
                "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) " +
                "VALUES ($id, 'tenant-1', $name, $login, $role, $hash, $discount, $auth, 1, 'x')",
                ("$id", id), ("$name", name), ("$login", login), ("$role", role), ("$hash", hash),
                ("$discount", discount), ("$auth", auth));
        }

        _journal = new SqliteTefJournal(_file.Path);
        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
        _screen = new SaleViewModel(
            new ItemRegistration(_database, Terminal, ledger),
            new Catalog(_database.Connection, Terminal.TenantId),
            new Checkout(_database, Terminal, ledger, new TefCoordinator(new TefSimulator(), _journal)),
            new Identity("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m),
            adjustments: new SaleAdjustments(_database, Terminal, ledger),
            authorization: new StaffAuthentication(_database, Terminal.TenantId),
            stableWeight: () => _plate)
        {
            AskText = _ => Task.FromResult(_answers.Count > 0 ? _answers.Dequeue() : null),
            AskAuthorizer = request =>
            {
                _requests.Add(request);
                if (_credentials is not { } typed) return Task.FromResult<Identity?>(null);
                try
                {
                    return Task.FromResult<Identity?>(request.Authorize(typed.Login, typed.Pin));
                }
                catch (AuthenticationException error)
                {
                    _dialogError = error.Message;
                    return Task.FromResult<Identity?>(null);
                }
            },
        };
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private Product Product(string id) => new Catalog(_database.Connection, Terminal.TenantId).Get(id)!;

    private void Put(long grams) =>
        _plate = new ToledoPrix3Protocol().Parse(System.Text.Encoding.ASCII.GetBytes(grams.ToString("00000")), DateTimeOffset.UtcNow);

    private void AddTortaAndRefri()
    {
        Put(847);
        _screen.AddCommand.Execute(Product("p-torta-kg"));
        _screen.AddCommand.Execute(Product("p-refri"));
    }

    [Fact]
    public void A_weighed_product_waits_for_a_stable_weight()
    {
        _screen.AddCommand.Execute(Product("p-torta-kg"));
        Assert.Equal("Aguarde a balança estabilizar antes de registrar o item.", _screen.Error);
        Assert.Empty(_screen.Lines);
    }

    [Fact]
    public void A_stable_weight_becomes_a_line_in_kilos()
    {
        Put(847);
        _screen.AddCommand.Execute(Product("p-torta-kg"));

        var line = Assert.Single(_screen.Lines);
        Assert.Equal("0,847 kg", line.Quantity);
        Assert.Equal("R$ 42,27", _screen.Total);
    }

    [Fact]
    public void The_scale_display_follows_the_readings()
    {
        _screen.ShowReading(new ScaleReading(ScaleStatus.Unstable, 0, "IIIII", DateTimeOffset.UtcNow));
        Assert.Equal("Balança: estabilizando…", _screen.ScaleText);
        Assert.False(_screen.ScaleStable);

        Put(1250);
        _screen.ShowReading(_plate!);
        Assert.Equal("Balança: 1,250 kg", _screen.ScaleText);
        Assert.True(_screen.ScaleStable);
    }

    [Fact]
    public async Task F4_cancels_the_marked_item_with_the_managers_pin()
    {
        AddTortaAndRefri();
        _screen.SelectedLine = _screen.Lines[0];
        _answers.Enqueue("cliente desistiu");

        await _screen.CancelItemCommand.ExecuteAsync(null);

        Assert.Equal(["Refrigerante lata"], _screen.Lines.Select(line => line.Name));
        Assert.Equal("R$ 3,33", _screen.Total);
        Assert.Equal("Item cancelado — autorizado por Bruno Gerente", _screen.Notice);
        // Só quem pode liberar cancelamento aparece no diálogo.
        Assert.Equal(["bruno"], _requests.Single().Logins);
        Assert.StartsWith("Cancelar Mousse a granel — R$", _requests.Single().Operation);
    }

    [Fact]
    public async Task An_owners_pin_does_not_cancel_and_the_item_stays()
    {
        AddTortaAndRefri();
        _screen.SelectedLine = _screen.Lines[0];
        _answers.Enqueue("teste");
        _credentials = ("carla", Pin);

        await _screen.CancelItemCommand.ExecuteAsync(null);

        Assert.Equal("Esta operação exige a credencial de um gerente.", _dialogError);
        Assert.Equal(2, _screen.Lines.Count);
        Assert.Equal(0L, _database.Scalar("SELECT COUNT(*) FROM order_items WHERE canceled_at IS NOT NULL"));
    }

    [Fact]
    public async Task No_reason_no_cancel_and_no_pin_asked()
    {
        AddTortaAndRefri();
        _screen.SelectedLine = _screen.Lines[0];
        _answers.Enqueue("   ");

        await _screen.CancelItemCommand.ExecuteAsync(null);

        Assert.Empty(_requests);
        Assert.Equal(2, _screen.Lines.Count);
    }

    [Fact]
    public void F4_needs_a_marked_item()
    {
        AddTortaAndRefri();
        Assert.False(_screen.CancelItemCommand.CanExecute(null));
        _screen.SelectedLine = _screen.Lines[1];
        Assert.True(_screen.CancelItemCommand.CanExecute(null));
    }

    [Fact]
    public async Task F6_gives_a_discount_within_the_authorizers_ceiling()
    {
        AddTortaAndRefri(); // 4227 + 333 = 4560
        _answers.Enqueue("10");
        _answers.Enqueue("cliente fiel");

        await _screen.DiscountCommand.ExecuteAsync(null);

        Assert.Equal("R$ 41,04", _screen.Total); // 4560 − 456
        Assert.Equal("Subtotal R$ 45,60 · desconto −R$ 4,56", _screen.Discount);
        Assert.Equal("Desconto de R$ 4,56 autorizado por Bruno Gerente", _screen.Notice);
        Assert.Equal(["bruno", "carla"], _requests.Single().Logins);
    }

    [Fact]
    public async Task Above_the_ceiling_the_dialog_says_so_and_nothing_changes()
    {
        AddTortaAndRefri();
        _answers.Enqueue("31,5");

        await _screen.DiscountCommand.ExecuteAsync(null);

        Assert.Equal("Bruno Gerente pode conceder até 30% — o pedido é de 31.5%.", _dialogError);
        Assert.Equal("R$ 45,60", _screen.Total);
        Assert.Null(_screen.Discount);
    }

    [Theory]
    [InlineData("abc")]
    [InlineData("0")]
    [InlineData("150")]
    public async Task A_percent_that_makes_no_sense_is_refused_before_the_pin(string typed)
    {
        AddTortaAndRefri();
        _answers.Enqueue(typed);

        await _screen.DiscountCommand.ExecuteAsync(null);

        Assert.Equal("Informe um percentual entre 0 e 100.", _screen.Error);
        Assert.Empty(_requests);
    }

    [Fact]
    public async Task The_discounted_total_is_what_the_cash_payment_closes()
    {
        AddTortaAndRefri();
        _answers.Enqueue("10");
        _answers.Enqueue("fiel");
        await _screen.DiscountCommand.ExecuteAsync(null);

        _screen.CashReceived = "50";
        await _screen.PayCashCommand.ExecuteAsync(null);

        Assert.Equal("Venda finalizada. Troco: R$ 8,96", _screen.Notice);
        Assert.Null(_screen.Discount);
        Assert.Equal(456L, _database.Scalar("SELECT discount_cents FROM orders"));
    }
}
