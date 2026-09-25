using Pdv.App;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>A tela de venda, sem janela, sobre o banco real e o simulador de TEF.</summary>
public sealed class SaleViewModelTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly TefSimulator _tef = new();
    private readonly SaleViewModel _screen;

    public SaleViewModelTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _journal = new SqliteTefJournal(_file.Path);
        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
        _screen = new SaleViewModel(
            new ItemRegistration(_database, Terminal, ledger),
            new Catalog(_database.Connection, Terminal.TenantId),
            new Checkout(_database, Terminal, ledger, new TefCoordinator(_tef, _journal)),
            new Identity("user-1", "Ana Caixa", "ana", "cashier", false, 0m));
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private void Scan(string text)
    {
        _screen.Query = text;
        _screen.ScanCommand.Execute(null);
    }

    [Fact]
    public void A_barcode_adds_the_item_and_clears_the_field()
    {
        Scan("7890000000011");
        var line = Assert.Single(_screen.Lines);
        Assert.Equal("Fatia de torta", line.Name);
        Assert.Equal("R$ 14,50", _screen.Total);
        Assert.Equal("", _screen.Query);
    }

    [Fact]
    public void A_name_lists_the_matches_to_pick_from()
    {
        Scan("refri");
        var product = Assert.Single(_screen.Results);
        _screen.AddCommand.Execute(product);
        Assert.Equal("R$ 3,33", _screen.Total);
    }

    [Fact]
    public void Nothing_found_says_so()
    {
        Scan("pizza");
        Assert.Equal("Nenhum produto encontrado para \"pizza\".", _screen.Error);
        Assert.Empty(_screen.Lines);
    }

    [Fact]
    public void A_weighed_product_is_refused_with_the_reason()
    {
        Scan("quilo");
        _screen.AddCommand.Execute(Assert.Single(_screen.Results));
        Assert.Contains("balança", _screen.Error);
        Assert.Empty(_screen.Lines);
    }

    [Fact]
    public void Nothing_to_pay_before_the_first_item()
    {
        Assert.False(_screen.PayCashCommand.CanExecute(null));
        Scan("7890000000028");
        Assert.True(_screen.PayCashCommand.CanExecute(null));
    }

    [Fact]
    public async Task Cash_shows_the_change_and_starts_a_new_sale()
    {
        Scan("7890000000011");
        _screen.CashReceived = "20,00";
        Assert.Equal("Troco: R$ 5,50", _screen.Change);

        await _screen.PayCashCommand.ExecuteAsync(null);

        Assert.Equal("Venda finalizada. Troco: R$ 5,50", _screen.Notice);
        Assert.Empty(_screen.Lines);
        Assert.Null(_screen.OrderId);
        Assert.Equal(1L, _database.Scalar("SELECT COUNT(*) FROM orders WHERE status = 'paid'"));
    }

    [Fact]
    public async Task Too_little_cash_says_how_much_is_missing()
    {
        Scan("7890000000011");
        _screen.CashReceived = "10";
        await _screen.PayCashCommand.ExecuteAsync(null);
        Assert.Equal("Faltam R$ 4,50 para fechar a venda.", _screen.Error);
        Assert.NotNull(_screen.OrderId);
    }

    [Fact]
    public async Task A_card_sale_shows_the_tef_conversation()
    {
        Scan("7890000000011");
        await _screen.PayCardCommand.ExecuteAsync(TefCardType.Debit);

        Assert.Equal("Venda finalizada.", _screen.Notice);
        Assert.Contains("Insira, aproxime ou passe o cartão", _screen.TefMessages);
        Assert.Contains("Transação aprovada", _screen.TefMessages);
        Assert.Equal(HostState.Confirmed, Assert.Single(_tef.Host).Value);
    }

    [Fact]
    public async Task A_declined_card_keeps_the_sale_to_try_another_way()
    {
        // O simulador nega valores terminados em ,51.
        _database.Execute("UPDATE products SET price_cents = 1051 WHERE id = 'p-refri'");
        Scan("7890000000028");
        await _screen.PayCardCommand.ExecuteAsync(TefCardType.Debit);

        Assert.Equal("Cartão negado: Saldo insuficiente", _screen.Error);
        Assert.Single(_screen.Lines);
        Assert.NotNull(_screen.OrderId);

        await _screen.PayCashCommand.ExecuteAsync(null);
        Assert.Equal("Venda finalizada.", _screen.Notice);
    }

    [Theory]
    [InlineData("12,50", 1250L)]
    [InlineData("12.50", 1250L)]
    [InlineData("R$ 1.234,56", 123456L)]
    [InlineData("20", 2000L)]
    [InlineData("", null)]
    [InlineData("abc", null)]
    [InlineData("-5", null)]
    public void Money_typed_the_way_people_type_it(string text, long? cents) => Assert.Equal(cents, Money.Parse(text));
}

public sealed class CounterOpeningTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly SqliteTefJournal _journal;
    private readonly TefSimulator _tef = new();

    public CounterOpeningTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        _journal = new SqliteTefJournal(_file.Path);
    }

    public void Dispose()
    {
        _journal.Dispose();
        _database.Dispose();
        _file.Dispose();
    }

    private SaleViewModel Screen()
    {
        var ledger = new AuditLedger(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);
        var tef = new TefCoordinator(_tef, _journal);
        return new SaleViewModel(
            new ItemRegistration(_database, Terminal, ledger),
            new Catalog(_database.Connection, Terminal.TenantId),
            new Checkout(_database, Terminal, ledger, tef),
            new Identity("user-1", "Ana Caixa", "ana", "cashier", false, 0m),
            token => tef.RecoverPendingAsync(entry => SaleRepository.WasRecorded(_database.Connection, entry.TransactionId), token));
    }

    [Fact]
    public async Task A_card_left_pending_by_a_crash_is_undone_and_the_operator_is_told()
    {
        // Ontem: cartão aprovado, e o caixa caiu antes de gravar a venda.
        var approval = Assert.IsType<TefOutcome.Approved>(
            await new TefCoordinator(_tef, _journal).AuthorizeAsync("pedido-perdido", 2500, TefCardType.Credit, new Quiet())).Approval;

        var screen = Screen();
        await screen.StartCommand.ExecuteAsync(null);

        var message = Assert.Single(screen.Recoveries);
        Assert.Contains("DESFEITA", message);
        Assert.Equal(HostState.Undone, _tef.Host[approval.TransactionId]);
    }

    [Fact]
    public async Task After_the_opening_the_first_card_of_the_day_goes_through()
    {
        await new TefCoordinator(_tef, _journal).AuthorizeAsync("pedido-perdido", 2500, TefCardType.Credit, new Quiet());
        var screen = Screen();
        await screen.StartCommand.ExecuteAsync(null);

        screen.Query = "7890000000011";
        screen.ScanCommand.Execute(null);
        await screen.PayCardCommand.ExecuteAsync(TefCardType.Debit);
        Assert.Equal("Venda finalizada.", screen.Notice);
    }

    [Fact]
    public async Task A_clean_opening_says_nothing()
    {
        var screen = Screen();
        await screen.StartCommand.ExecuteAsync(null);
        Assert.Empty(screen.Recoveries);
    }

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
