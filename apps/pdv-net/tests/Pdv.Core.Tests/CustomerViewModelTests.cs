using System.Text.Json;
using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Customers;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>A tela de venda com cliente: cashback, pré-pago, fiado e níveis (C6b), sem janela.</summary>
public sealed class CustomerViewModelTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private static readonly JsonElement Pins =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.GetProperty("hashes")[0].Clone();

    private static readonly string Pin = Pins.GetProperty("pin").GetString()!;

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly CustomerLedgers _customers;
    private readonly DiscountTierService _tiers;
    private readonly SaleViewModel _screen;
    private readonly Queue<string?> _answers = new();
    private readonly Queue<int?> _options = new();
    private readonly List<AuthorizationRequest> _requests = [];
    private (string Login, string Pin)? _credentials = ("bruno", Pin);

    public CustomerViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) VALUES " +
            "('u-caixa', 'tenant-1', 'Ana Caixa', 'ana', 'cashier', $h, '0', 0, 1, 'x'), " +
            "('u-gerente', 'tenant-1', 'Bruno Gerente', 'bruno', 'manager', $h, '30', 1, 1, 'x'), " +
            "('u-dona', 'tenant-1', 'Carla Dona', 'carla', 'owner', $h, '100', 1, 1, 'x')",
            ("$h", Pins.GetProperty("hash").GetString()));
        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
        _journal = new SqliteTefJournal(_file.Path);
        _customers = new CustomerLedgers(_database, Terminal, ledger);
        _tiers = new DiscountTierService(_database, Terminal, ledger);
        var auth = new StaffAuthentication(_database, "tenant-1");
        _screen = new SaleViewModel(
            new ItemRegistration(_database, Terminal, ledger),
            new Catalog(_database.Connection, "tenant-1"),
            new Checkout(_database, Terminal, ledger, new TefCoordinator(new TefSimulator(), _journal), customers: _customers),
            new Identity("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m),
            adjustments: new SaleAdjustments(_database, Terminal, ledger),
            authorization: auth,
            customers: _customers,
            tiers: _tiers)
        {
            AskText = _ => Task.FromResult(_answers.Count > 0 ? _answers.Dequeue() : null),
            AskOption = (_, _) => Task.FromResult(_options.Count > 0 ? _options.Dequeue() : null),
            AskAuthorizer = request =>
            {
                _requests.Add(request);
                return Task.FromResult<Identity?>(_credentials is { } typed ? request.Authorize(typed.Login, typed.Pin) : null);
            },
        };
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private void Sell(string productId = "p-fatia") =>
        _screen.AddCommand.Execute(new Catalog(_database.Connection, "tenant-1").Get(productId)!);

    private async Task Identify(params string?[] answers)
    {
        foreach (var answer in answers) _answers.Enqueue(answer);
        await _screen.IdentifyCustomerCommand.ExecuteAsync(null);
    }

    [Fact]
    public async Task A_known_phone_identifies_the_customer_with_the_balances()
    {
        var lia = _customers.CreateCustomer("Lia Cliente", "21998765432");
        _customers.Deposit(lia, 2000, "u-caixa", "u-gerente");

        await Identify("(21) 99876-5432");

        Assert.Equal(lia, _screen.Customer!.Id);
        Assert.Equal("Lia Cliente — cashback R$ 0,00 · pré-pago R$ 20,00", _screen.CustomerSummary);
    }

    [Fact]
    public async Task An_unknown_phone_registers_the_customer_on_the_spot()
    {
        await Identify("21 91234 5678", "Nova Pessoa");
        Assert.Equal("Nova Pessoa", _customers.FindByPhone("21912345678")!.Name);
        Assert.Equal("Nova Pessoa", _screen.Customer!.Name);
    }

    [Fact]
    public async Task Prepaid_is_offered_only_with_a_customer_and_pays_the_sale()
    {
        Sell();
        Assert.False(_screen.PayPrepaidCommand.CanExecute(null));
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.Deposit(lia, 2000, "u-caixa", "u-gerente");
        _customers.ConfigureCashback(10m, 0, 30, "u-gerente");
        await Identify("1");
        Assert.True(_screen.PayPrepaidCommand.CanExecute(null));

        await _screen.PayPrepaidCommand.ExecuteAsync(null);

        Assert.Equal("Venda finalizada. Cashback: R$ 1,45. Saldo pré-pago: R$ 5,50.", _screen.Notice);
        Assert.Null(_screen.Customer); // a próxima venda começa sem cliente
    }

    [Fact]
    public async Task Credit_beyond_the_limit_shows_the_reason_and_keeps_the_sale()
    {
        Sell();
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.ConfigureCreditAccount(lia, 1000, 30, "u-caixa", "u-gerente");
        await Identify("1");

        await _screen.PayCreditAccountCommand.ExecuteAsync(null);

        Assert.Equal("Limite disponível do fiado é insuficiente.", _screen.Error);
        Assert.Single(_screen.Lines);
    }

    [Fact]
    public async Task The_customer_tier_applies_at_payment_with_the_pin_it_requires()
    {
        var diamond = _tiers.Configure("diamond", "Diamante", 20m, 0, true, "u-dona");
        var lia = _customers.CreateCustomer("Lia", "1");
        _tiers.Assign(lia, diamond.Id, "u-gerente");
        Sell();
        await Identify("1");

        await _screen.PayCardCommand.ExecuteAsync(TefCardType.Debit);

        Assert.Equal("Autorizar nível Diamante para Lia.", _requests.Single().Operation);
        Assert.Equal(1160, Convert.ToInt64(_database.Scalar("SELECT amount_cents FROM payments"))); // 1450 − 20%
        Assert.StartsWith("Venda finalizada.", _screen.Notice);
    }

    [Fact]
    public async Task Without_the_tier_pin_nothing_is_charged()
    {
        var owner = _tiers.Configure("owner", "Dono", 50m, 0, true, "u-dona");
        var lia = _customers.CreateCustomer("Lia", "1");
        _tiers.Assign(lia, owner.Id, "u-dona");
        Sell();
        await Identify("1");
        _credentials = null;

        await _screen.PayCashCommand.ExecuteAsync(null);

        Assert.Equal(["carla"], _requests.Single().Logins);
        Assert.Equal(0L, _database.Scalar("SELECT COUNT(*) FROM payments"));
    }

    [Fact]
    public async Task F7_configures_cashback_with_the_managers_pin()
    {
        _answers.Enqueue("5");
        _answers.Enqueue("10,00");
        _answers.Enqueue("30");
        await _screen.ConfigureCashbackCommand.ExecuteAsync(null);

        Assert.Equal("Cashback de 5% configurado por Bruno Gerente", _screen.Notice);
        Assert.Equal(500L, _database.Scalar("SELECT percent_basis_points FROM cashback_rules"));
        Assert.Equal(1000L, _database.Scalar("SELECT max_per_sale_cents FROM cashback_rules"));
    }

    [Fact]
    public async Task F11_loads_prepaid_credit_for_the_customer()
    {
        _customers.CreateCustomer("Lia", "1");
        _answers.Enqueue("1");
        _answers.Enqueue("50");
        await _screen.DepositPrepaidCommand.ExecuteAsync(null);

        Assert.Equal("Carga concluída. Saldo de Lia: R$ 50,00", _screen.Notice);
    }

    [Fact]
    public async Task F5_sets_the_limit_then_receives_a_payment()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        await Identify("1");
        _options.Enqueue(0);
        _answers.Enqueue("200");
        _answers.Enqueue("30");
        await _screen.ManageCreditAccountCommand.ExecuteAsync(null);
        Assert.Equal("Em aberto: R$ 0,00 · disponível: R$ 200,00 · vencido: R$ 0,00", _screen.Notice);

        Sell();
        await _screen.PayCreditAccountCommand.ExecuteAsync(null);
        await Identify("1");
        _options.Enqueue(1);
        _answers.Enqueue("4,50");
        await _screen.ManageCreditAccountCommand.ExecuteAsync(null);
        Assert.Equal(1000, _customers.CreditPositionOf(lia).OutstandingCents);
    }

    [Fact]
    public async Task Ctrl_F6_assigning_a_protected_tier_asks_only_owners()
    {
        _tiers.Configure("employee", "Funcionário", 20m, 0, true, "u-dona");
        _customers.CreateCustomer("Lia", "1");
        await Identify("1");
        _options.Enqueue(1);
        _options.Enqueue(0);
        _credentials = ("carla", Pin);

        await _screen.ManageTiersCommand.ExecuteAsync(null);

        Assert.Equal(["carla"], _requests.Single().Logins);
        Assert.EndsWith("Esta classificação é permanente.", _requests.Single().Operation);
        Assert.Equal("Lia: nível Funcionário", _screen.Notice);
    }
}
