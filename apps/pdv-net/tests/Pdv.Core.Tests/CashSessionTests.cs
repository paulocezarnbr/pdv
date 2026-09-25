using System.Text.Json;
using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>Abertura e fechamento cego do caixa (C6a), sobre o banco real e pela tela sem janela.</summary>
public sealed class CashSessionTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private static readonly JsonElement Pins =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("pin-hashes.json"))).RootElement.GetProperty("hashes")[0].Clone();

    private static readonly Identity Ana = new("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m);
    private static readonly Identity Bruno = new("u-gerente", "Bruno Gerente", "bruno", "manager", true, 30m);

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly AuditLedger _ledger = new(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
    private readonly SqliteTefJournal _journal;

    public CashSessionTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _database.Execute(
            "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) VALUES " +
            "('u-caixa', 'tenant-1', 'Ana Caixa', 'ana', 'cashier', $h, '0', 0, 1, 'x'), " +
            "('u-gerente', 'tenant-1', 'Bruno Gerente', 'bruno', 'manager', $h, '30', 1, 1, 'x')",
            ("$h", Pins.GetProperty("hash").GetString()));
        _journal = new SqliteTefJournal(_file.Path);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private CashSessionService Sessions() => new(_database, Terminal, _ledger);

    private long Scalar(string sql) => Convert.ToInt64(_database.Scalar(sql));

    /// <summary>Uma venda de verdade, paga em dinheiro com troco, pelo fechamento do PDV.</summary>
    private async Task SellForCash(long receivedCents)
    {
        var item = new ItemRegistration(_database, Terminal, _ledger)
            .RegisterUnitItem(null, new Catalog(_database.Connection, "tenant-1").Get("p-fatia")!, 1m, "u-caixa");
        await new Checkout(_database, Terminal, _ledger, new TefCoordinator(new TefSimulator(), _journal))
            .CloseAsync(item.Order.Id, "u-caixa", [PaymentIntent.Cash(receivedCents)], new Quiet());
    }

    // -- serviço -------------------------------------------------------------

    [Fact]
    public void The_open_session_never_shows_the_expected_amount()
    {
        var opened = Sessions().Open("u-caixa", 10_000);
        Assert.Equal(10_000, opened.OpeningCents);
        Assert.DoesNotContain(typeof(OpenCashSession).GetProperties(), property => property.Name.Contains("Expected"));
        Assert.Equal(opened, Sessions().Current());
    }

    [Fact]
    public async Task The_blind_close_counts_cash_minus_change()
    {
        Sessions().Open("u-caixa", 10_000);
        await SellForCash(2_000); // R$ 14,50 de fatia, R$ 20 recebidos: R$ 5,50 de troco

        var result = Sessions().Close(11_300, "u-caixa", "u-gerente");

        Assert.Equal(10_000 + 1_450, result.ExpectedCents);
        Assert.Equal(-150, result.DifferenceCents);
        Assert.Equal(11_300, Scalar($"SELECT declared_amount_cents FROM cash_sessions WHERE id = '{result.SessionId}'"));
        Assert.Equal(-150, Scalar($"SELECT difference_cents FROM cash_sessions WHERE id = '{result.SessionId}'"));
        Assert.Null(Sessions().Current());
    }

    [Fact]
    public async Task Only_cash_of_paid_sales_of_this_session_counts()
    {
        // Dinheiro da sessão anterior: não é desta gaveta.
        Sessions().Open("u-caixa", 0);
        await SellForCash(1_450);
        Sessions().Close(1_450, "u-caixa", "u-gerente");
        _database.Execute("UPDATE payments SET created_at = '2026-01-01T00:00:00.000+00:00'");

        Sessions().Open("u-caixa", 5_000);
        // Cartão não entra na gaveta.
        var card = new ItemRegistration(_database, Terminal, _ledger)
            .RegisterUnitItem(null, new Catalog(_database.Connection, "tenant-1").Get("p-refri")!, 1m, "u-caixa");
        await new Checkout(_database, Terminal, _ledger, new TefCoordinator(new TefSimulator(), _journal))
            .CloseAsync(card.Order.Id, "u-caixa", [PaymentIntent.Card(TefCardType.Debit, 333)], new Quiet());
        // Pedido não pago com um pagamento em dinheiro (estorno, erro de sincronização): fora.
        await SellForCash(1_450);
        _database.Execute(
            "UPDATE orders SET status = 'canceled' WHERE id = (SELECT order_id FROM payments ORDER BY created_at DESC, rowid DESC LIMIT 1)");

        Assert.Equal(5_000, Sessions().Close(5_000, "u-caixa", "u-gerente").ExpectedCents);
    }

    [Fact]
    public void The_close_is_one_transaction_with_audit_and_outbox_in_the_push_contract()
    {
        var opened = Sessions().Open("u-caixa", 0);
        Sessions().Close(0, "u-caixa", "u-gerente");

        Assert.Equal("info", _database.Scalar("SELECT severity FROM audit_ledger WHERE event_type = 'session_closed'"));
        Assert.Equal("u-gerente", _database.Scalar("SELECT authorizer_user_id FROM audit_ledger WHERE event_type = 'session_closed'"));
        var payload = JsonDocument.Parse((string)_database.Scalar(
            $"SELECT payload_json FROM sync_outbox WHERE entity_table = 'cash_sessions' AND entity_id = '{opened.Id}'")!).RootElement;
        Assert.True(payload.GetProperty("blind_close").GetBoolean());

        var contract = JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("push-day.json"))).RootElement
            .GetProperty("items").EnumerateArray()
            .First(item => item.GetProperty("entity_table").GetString() == "cash_sessions").GetProperty("payload");
        Assert.Equal(contract.EnumerateObject().Select(p => p.Name).Order(), payload.EnumerateObject().Select(p => p.Name).Order());
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void A_divergence_is_a_warning_in_the_ledger()
    {
        Sessions().Open("u-caixa", 5_000);
        Sessions().Close(4_900, "u-caixa", "u-gerente");
        Assert.Equal("warning", _database.Scalar("SELECT severity FROM audit_ledger WHERE event_type = 'session_closed'"));
    }

    [Fact]
    public void A_second_operator_cannot_take_an_open_drawer()
    {
        Sessions().Open("u-caixa", 0);
        var error = Assert.Throws<CashSessionException>(() => Sessions().Open("u-gerente", 0));
        Assert.Contains("outro operador", error.Message);
        Assert.Equal(Sessions().Current()!.Id, Sessions().Open("u-caixa", 999).Id); // o mesmo segue na dele
    }

    [Fact]
    public void Negative_values_and_closing_twice_are_refused()
    {
        Assert.Throws<CashSessionException>(() => Sessions().Open("u-caixa", -1));
        Sessions().Open("u-caixa", 0);
        Assert.Throws<CashSessionException>(() => Sessions().Close(-1, "u-caixa"));
        Sessions().Close(0, "u-caixa");
        Assert.Equal("Não há caixa aberto.", Assert.Throws<CashSessionException>(() => Sessions().Close(0, "u-caixa")).Message);
    }

    [Fact]
    public void An_authorizer_revoked_after_the_pin_does_not_close()
    {
        Sessions().Open("u-caixa", 0);
        _database.Execute("UPDATE users SET can_authorize = 0 WHERE id = 'u-gerente'");
        Assert.Throws<CashSessionException>(() => Sessions().Close(0, "u-caixa", "u-gerente"));
        Assert.NotNull(Sessions().Current());
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM audit_ledger WHERE event_type = 'session_closed'"));
    }

    // -- abertura pela tela --------------------------------------------------

    [Fact]
    public async Task Opening_asks_for_the_float_until_it_is_a_value()
    {
        var answers = new Queue<string?>(["abc", "150,00"]);
        var prompts = new List<string>();
        var (outcome, _) = await new CashOpening(Sessions()).EnsureOpenAsync(Ana, prompt =>
        {
            prompts.Add(prompt);
            return Task.FromResult(answers.Dequeue());
        });

        Assert.Equal(CashOpening.Outcome.Ready, outcome);
        Assert.Equal(15_000, Sessions().Current()!.OpeningCents);
        Assert.StartsWith("\"abc\" não é um valor.", prompts[1]);
    }

    [Fact]
    public async Task Another_operators_drawer_blocks_and_canceling_opens_nothing()
    {
        var (canceled, _) = await new CashOpening(Sessions()).EnsureOpenAsync(Ana, _ => Task.FromResult<string?>(null));
        Assert.Equal(CashOpening.Outcome.Canceled, canceled);
        Assert.Null(Sessions().Current());

        Sessions().Open("u-gerente", 0);
        var (blocked, message) = await new CashOpening(Sessions()).EnsureOpenAsync(Ana, _ => Task.FromResult<string?>("0"));
        Assert.Equal(CashOpening.Outcome.Blocked, blocked);
        Assert.Equal(CashOpening.BlockedMessage, message);

        var (ready, _) = await new CashOpening(Sessions()).EnsureOpenAsync(Bruno, _ => throw new InvalidOperationException("não pergunta"));
        Assert.Equal(CashOpening.Outcome.Ready, ready);
    }

    // -- fechamento pela tela (F12) -----------------------------------------

    private SaleViewModel Screen(Queue<string?> answers, (string, string)? credentials)
    {
        var auth = new StaffAuthentication(_database, "tenant-1");
        return new SaleViewModel(
            new ItemRegistration(_database, Terminal, _ledger),
            new Catalog(_database.Connection, "tenant-1"),
            new Checkout(_database, Terminal, _ledger, new TefCoordinator(new TefSimulator(), _journal)),
            Ana,
            adjustments: new SaleAdjustments(_database, Terminal, _ledger),
            authorization: auth,
            cashSessions: Sessions())
        {
            AskText = _ => Task.FromResult(answers.Count > 0 ? answers.Dequeue() : null),
            AskAuthorizer = request => Task.FromResult<Identity?>(
                credentials is { } typed ? request.Authorize(typed.Item1, typed.Item2) : null),
        };
    }

    [Fact]
    public async Task F12_counts_first_then_the_pin_then_shows_the_result()
    {
        Sessions().Open("u-caixa", 10_000);
        var screen = Screen(new Queue<string?>(["100,50"]), ("bruno", Pins.GetProperty("pin").GetString()!));
        CashReconciliation? closed = null;
        screen.CashClosed += (_, result) => closed = result;

        await screen.CloseCashCommand.ExecuteAsync(null);

        Assert.NotNull(closed);
        Assert.Equal("Declarado: R$ 100,50\nEsperado: R$ 100,00\nDivergência: +R$ 0,50", SaleViewModel.Describe(closed));
        Assert.Null(Sessions().Current());
    }

    [Fact]
    public async Task F12_with_a_sale_on_the_screen_is_refused()
    {
        Sessions().Open("u-caixa", 0);
        var screen = Screen(new Queue<string?>(["0"]), ("bruno", Pins.GetProperty("pin").GetString()!));
        screen.AddCommand.Execute(new Catalog(_database.Connection, "tenant-1").Get("p-refri")!);

        await screen.CloseCashCommand.ExecuteAsync(null);

        Assert.Equal("Finalize ou cancele a venda aberta antes de fechar o caixa.", screen.Error);
        Assert.NotNull(Sessions().Current());
    }

    [Fact]
    public async Task F12_without_the_pin_closes_nothing()
    {
        Sessions().Open("u-caixa", 0);
        var screen = Screen(new Queue<string?>(["0"]), null);
        await screen.CloseCashCommand.ExecuteAsync(null);
        Assert.NotNull(Sessions().Current());
    }

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
