using System.Text.Json;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Customers;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>Clientes, cashback, pré-pago, fiado e níveis de desconto (C6b), sobre o banco real.</summary>
public sealed class CustomerLedgerTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private static readonly JsonElement PushDay =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("push-day.json"))).RootElement.Clone();

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 9, 25, 12, 0, 0, TimeSpan.Zero));
    private readonly AuditLedger _ledger;
    private readonly SqliteTefJournal _journal;
    private readonly TefSimulator _tef = new();
    private readonly CustomerLedgers _customers;
    private readonly DiscountTierService _tiers;

    public CustomerLedgerTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) VALUES " +
            "('u-caixa', 'tenant-1', 'Ana Caixa', 'ana', 'cashier', NULL, '0', 0, 1, 'x'), " +
            "('u-gerente', 'tenant-1', 'Bruno Gerente', 'bruno', 'manager', NULL, '30', 1, 1, 'x'), " +
            "('u-dona', 'tenant-1', 'Carla Dona', 'carla', 'owner', NULL, '100', 1, 1, 'x')");
        _ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret, _clock);
        _journal = new SqliteTefJournal(_file.Path);
        _customers = new CustomerLedgers(_database, Terminal, _ledger, _clock);
        _tiers = new DiscountTierService(_database, Terminal, _ledger, _clock);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private long Scalar(string sql) => Convert.ToInt64(_database.Scalar(sql));

    private ItemResult Sale(string productId = "p-fatia") =>
        new ItemRegistration(_database, Terminal, _ledger, clock: _clock)
            .RegisterUnitItem(null, new Catalog(_database.Connection, "tenant-1").Get(productId)!, 1m, "u-caixa");

    private Task<CheckoutResult> Close(string orderId, IReadOnlyList<PaymentIntent> intents, string? customerId) =>
        new Checkout(_database, Terminal, _ledger, new TefCoordinator(_tef, _journal), _clock, _customers)
            .CloseAsync(orderId, "u-caixa", intents, new Quiet(), customerId: customerId);

    // -- clientes e cashback -------------------------------------------------

    [Fact]
    public void A_customer_is_found_by_the_digits_of_the_phone()
    {
        var id = _customers.CreateCustomer("  Lia Cliente ", "(21) 99876-5432");
        Assert.Equal(new Customer(id, "Lia Cliente", "21998765432"), _customers.FindByPhone("21 99876 5432"));
        Assert.Null(_customers.FindByPhone("sem número"));
        Assert.Throws<CustomerException>(() => _customers.CreateCustomer("  ", null));
    }

    [Fact]
    public async Task A_sale_with_the_customer_earns_cashback_once_rounded_half_up()
    {
        _customers.ConfigureCashback(5m, 0, 30, "u-gerente");
        var lia = _customers.CreateCustomer("Lia", "21998765432");
        var sale = Sale(); // R$ 14,50 → 5% = 72,5 → 73

        var closed = Assert.IsType<CheckoutResult.Closed>(await Close(sale.Order.Id, [PaymentIntent.Cash(1450)], lia));

        Assert.Equal(73, closed.Cashback!.AmountCents);
        Assert.Equal("2026-10-25T12:00:00.000+00:00", closed.Cashback.ExpiresAt);
        Assert.Equal(73, _customers.CashbackBalance(lia));
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'cashback_credited'"));
        // O mesmo pedido de novo não credita outra vez.
        Assert.Equal(closed.Cashback, _database.InTransaction(tx => _customers.EarnWithin(tx, lia, sale.Order.Id, 1450, "u-caixa")));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public async Task The_cap_limits_and_expired_credit_leaves_the_balance()
    {
        _customers.ConfigureCashback(50m, 100, 1, "u-gerente");
        var lia = _customers.CreateCustomer("Lia", "1");
        await Close(Sale().Order.Id, [PaymentIntent.Cash(1450)], lia);
        Assert.Equal(100, _customers.CashbackBalance(lia));

        _clock.Advance(TimeSpan.FromDays(1) + TimeSpan.FromSeconds(1));
        Assert.Equal(0, _customers.CashbackBalance(lia));
    }

    [Fact]
    public async Task Redeeming_takes_what_expires_first_one_debit_per_lot()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.ConfigureCashback(10m, 0, 30, "u-gerente");
        await Close(Sale().Order.Id, [PaymentIntent.Cash(1450)], lia); // 145, vence em 30 dias
        _customers.ConfigureCashback(10m, 0, 5, "u-gerente");
        await Close(Sale("p-refri").Order.Id, [PaymentIntent.Cash(333)], lia); // 33, vence em 5 dias

        _customers.RedeemCashback(lia, "pedido-x", 50, "u-caixa");

        Assert.Equal(128, _customers.CashbackBalance(lia));
        Assert.Equal([33L, 17L], ReadLongs("SELECT amount_cents FROM cashback_ledger WHERE entry_type = 'debit' ORDER BY rowid"));
        Assert.Throws<CustomerException>(() => _customers.RedeemCashback(lia, "pedido-y", 129, "u-caixa"));
    }

    // -- pré-pago ------------------------------------------------------------

    [Fact]
    public async Task Prepaid_pays_inside_the_sale_transaction()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        Assert.Equal(2000, _customers.Deposit(lia, 2000, "u-caixa", "u-gerente"));
        Assert.Equal("warning", _database.Scalar("SELECT severity FROM audit_ledger WHERE event_type = 'prepaid_credited'"));

        var closed = Assert.IsType<CheckoutResult.Closed>(await Close(Sale().Order.Id, [PaymentIntent.Prepaid(1450)], lia));

        Assert.Equal(550, closed.PrepaidBalanceCents);
        Assert.Equal("prepaid", _database.Scalar("SELECT method FROM payments"));
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'prepaid_redeemed'"));
    }

    [Fact]
    public async Task Without_enough_prepaid_nothing_of_the_sale_is_written_and_the_card_is_undone()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.Deposit(lia, 1000, "u-caixa", "u-gerente");
        var sale = Sale();

        await Assert.ThrowsAsync<CustomerException>(() =>
            Close(sale.Order.Id, [PaymentIntent.Card(TefCardType.Debit, 449), PaymentIntent.Prepaid(1001)], lia));

        Assert.Equal("open", _database.Scalar($"SELECT status FROM orders WHERE id = '{sale.Order.Id}'"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM payments"));
        Assert.Equal(1000, _customers.PrepaidBalance(lia));
        Assert.Contains("Undone", ReadStrings("SELECT state FROM tef_transactions"));
    }

    [Theory]
    [InlineData("prepaid", "Crédito pré-pago exige cliente identificado.")]
    [InlineData("credit_account", "Fiado exige cliente identificado.")]
    public async Task Prepaid_and_credit_without_a_customer_are_refused_before_any_card(string method, string message)
    {
        var sale = Sale();
        var intent = method == "prepaid" ? PaymentIntent.Prepaid(450) : PaymentIntent.CreditAccount(450);

        var error = await Assert.ThrowsAsync<InsufficientPaymentException>(() =>
            Close(sale.Order.Id, [PaymentIntent.Card(TefCardType.Debit, 1000), intent], customerId: null));

        Assert.Equal(message, error.Message);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM tef_transactions"));
    }

    // -- fiado ---------------------------------------------------------------

    [Fact]
    public async Task Credit_within_the_limit_then_payment_settles_the_oldest_first()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.ConfigureCreditAccount(lia, 2000, 30, "u-caixa", "u-gerente");

        await Close(Sale().Order.Id, [PaymentIntent.CreditAccount(1450)], lia);
        Assert.Equal(new CreditPosition(2000, 1450, 550, 0), _customers.CreditPositionOf(lia));

        await Assert.ThrowsAsync<CustomerException>(() => Close(Sale().Order.Id, [PaymentIntent.CreditAccount(1450)], lia));

        await Close(Sale("p-refri").Order.Id, [PaymentIntent.CreditAccount(333)], lia);
        _clock.Advance(TimeSpan.FromDays(31));
        Assert.Equal(new CreditPosition(2000, 1783, 217, 1783), _customers.CreditPositionOf(lia));

        Assert.Equal(1783 - 1500, _customers.PayCredit(lia, 1500, "u-caixa").OutstandingCents);
        Assert.Equal([1450L, 50L], ReadLongs("SELECT amount_cents FROM credit_account_ledger WHERE entry_type = 'payment' ORDER BY rowid"));
        Assert.Throws<CustomerException>(() => _customers.PayCredit(lia, 284, "u-caixa"));
    }

    [Fact]
    public void Reconfiguring_the_account_goes_up_as_an_update_with_a_new_identity()
    {
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.ConfigureCreditAccount(lia, 1000, 30, "u-caixa", "u-gerente");
        _customers.ConfigureCreditAccount(lia, 3000, 15, "u-caixa", "u-gerente");

        Assert.Equal(["insert", "update"], ReadStrings(
            "SELECT operation FROM sync_outbox WHERE entity_table = 'customer_credit_accounts' ORDER BY seq"));
        Assert.Equal(2, Scalar("SELECT COUNT(DISTINCT client_uuid) FROM sync_outbox WHERE entity_table = 'customer_credit_accounts'"));
    }

    // -- níveis --------------------------------------------------------------

    [Fact]
    public void Owner_always_requires_the_password_even_if_configured_without()
    {
        var owner = _tiers.Configure("OWNER", "Dono", 50m, 0, requiresManager: false, "u-dona");
        Assert.True(owner.RequiresManager);
        Assert.Equal(5000, owner.PercentBasisPoints);

        var lia = _customers.CreateCustomer("Lia", "1");
        _tiers.Assign(lia, owner.Id, "u-dona");
        _database.Execute("UPDATE discount_tiers SET requires_manager = 0");
        Assert.Equal(Roles.OwnerOnly, _tiers.ForCustomer(lia)!.RequiredRoles);
    }

    [Fact]
    public void Protected_tiers_are_assigned_only_by_an_owner_and_never_leave()
    {
        var employee = _tiers.Configure("employee", "Funcionário", 20m, 0, true, "u-dona");
        var gold = _tiers.Configure("gold", "Ouro", 10m, 0, false, "u-dona");
        var lia = _customers.CreateCustomer("Lia", "1");

        Assert.Throws<CustomerException>(() => _tiers.Assign(lia, employee.Id, "u-gerente"));
        _tiers.Assign(lia, employee.Id, "u-dona");
        Assert.Throws<CustomerException>(() => _tiers.Assign(lia, gold.Id, "u-dona"));
        Assert.Equal("employee", _tiers.ForCustomer(lia)!.Code);
    }

    [Fact]
    public void A_tier_does_not_add_up_the_bigger_discount_wins()
    {
        var gold = _tiers.Configure("gold", "Ouro", 10m, 0, false, "u-dona");
        var sale = Sale(); // 1450 → 10% = 145
        Assert.Equal(145, _tiers.ApplyToOrder(sale.Order.Id, gold, "u-caixa", null));
        Assert.Equal("automatic_tier", JsonDocument.Parse((string)_database.Scalar(
            "SELECT payload_json FROM audit_ledger WHERE event_type = 'discount_applied'")!).RootElement.GetProperty("channel").GetString());

        var bronze = _tiers.Configure("bronze", "Bronze", 5m, 0, false, "u-dona");
        Assert.Equal(145, _tiers.ApplyToOrder(sale.Order.Id, bronze, "u-caixa", null));
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'discount_applied'"));
    }

    [Fact]
    public void A_tier_that_needs_a_manager_checks_the_role_in_the_transaction()
    {
        var diamond = _tiers.Configure("diamond", "Diamante", 20m, 0, true, "u-dona");
        var owner = _tiers.Configure("owner", "Dono", 30m, 0, true, "u-dona");
        var sale = Sale();

        Assert.Throws<CustomerException>(() => _tiers.ApplyToOrder(sale.Order.Id, diamond, "u-caixa", null));
        Assert.Throws<CustomerException>(() => _tiers.ApplyToOrder(sale.Order.Id, diamond, "u-caixa", "u-caixa"));
        Assert.Throws<CustomerException>(() => _tiers.ApplyToOrder(sale.Order.Id, owner, "u-caixa", "u-gerente"));
        Assert.Equal(290, _tiers.ApplyToOrder(sale.Order.Id, diamond, "u-caixa", "u-gerente"));
        Assert.Equal("u-gerente", _database.Scalar($"SELECT authorized_by_user_id FROM orders WHERE id = '{sale.Order.Id}'"));
    }

    // -- contrato de sincronização ------------------------------------------

    [Fact]
    public async Task Every_customer_table_goes_up_with_the_keys_of_the_push_contract()
    {
        _customers.ConfigureCashback(5m, 0, 30, "u-gerente");
        var lia = _customers.CreateCustomer("Lia", "1");
        _customers.Deposit(lia, 5000, "u-caixa", "u-gerente");
        _customers.ConfigureCreditAccount(lia, 5000, 30, "u-caixa", "u-gerente");
        _customers.ConfigureCreditAccount(lia, 6000, 30, "u-caixa", "u-gerente");
        await Close(Sale().Order.Id, [PaymentIntent.Prepaid(1000), PaymentIntent.CreditAccount(450)], lia);
        _customers.PayCredit(lia, 100, "u-caixa");
        var gold = _tiers.Configure("gold", "Ouro", 10m, 0, false, "u-dona");
        _tiers.Configure("gold", "Ouro+", 12m, 0, false, "u-dona");
        var silver = _tiers.Configure("silver", "Prata", 5m, 0, false, "u-dona");
        _tiers.Assign(lia, gold.Id, "u-gerente");
        _tiers.Assign(lia, silver.Id, "u-gerente");

        foreach (var table in new[]
                 {
                     "customers", "cashback_ledger", "prepaid_ledger", "credit_account_ledger", "customer_credit_accounts",
                     "discount_tiers", "customer_discount_tiers",
                 })
        {
            foreach (var operation in new[] { "insert", "update" })
            {
                var contract = PushDay.GetProperty("items").EnumerateArray().FirstOrDefault(item =>
                    item.GetProperty("entity_table").GetString() == table && item.GetProperty("operation").GetString() == operation);
                if (contract.ValueKind == JsonValueKind.Undefined) continue;
                var mine = ReadStrings(
                    $"SELECT payload_json FROM sync_outbox WHERE entity_table = '{table}' AND operation = '{operation}' LIMIT 1");
                Assert.True(mine.Count == 1, $"{table}/{operation} não foi para o outbox");
                Assert.Equal(Keys(contract.GetProperty("payload")), Keys(JsonDocument.Parse(mine[0]).RootElement));
            }
        }
        _ledger.Verify(_database.Connection);
    }

    private static List<string> Keys(JsonElement element) => element.EnumerateObject().Select(p => p.Name).Order().ToList();

    private List<long> ReadLongs(string sql)
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = sql;
        using var reader = command.ExecuteReader();
        var values = new List<long>();
        while (reader.Read()) values.Add(reader.GetInt64(0));
        return values;
    }

    private List<string> ReadStrings(string sql)
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = sql;
        using var reader = command.ExecuteReader();
        var values = new List<string>();
        while (reader.Read()) values.Add(reader.GetString(0));
        return values;
    }

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
