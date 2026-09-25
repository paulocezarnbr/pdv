using Pdv.Core.Tef;

namespace Pdv.Core.Tests;

/// <summary>
/// O ciclo do TEF medido pelo que importa: onde terminou o dinheiro do cliente
/// (o lado da adquirente, no simulador) e o que o diário do caixa sabe.
/// </summary>
public sealed class TefCoordinatorTests
{
    private readonly TefSimulator _tef = new();
    private readonly InMemoryTefJournal _journal = new();
    private readonly RecordingInteraction _ui = new();

    private TefCoordinator Coordinator() => new(_tef, _journal);

    private static TefApproval Approval(TefOutcome outcome) =>
        Assert.IsType<TefOutcome.Approved>(outcome).Approval;

    [Fact]
    public async Task An_approved_sale_is_confirmed_at_the_acquirer()
    {
        var coordinator = Coordinator();
        var approval = Approval(await coordinator.AuthorizeAsync("pedido-1", 4500, TefCardType.Debit, _ui));

        Assert.Equal(HostState.Authorized, _tef.Host[approval.TransactionId]);
        Assert.Equal(TefState.Approved, _journal.Find(approval.TransactionId)!.State);

        Assert.True(await coordinator.ConfirmAsync(approval));
        Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]);
        Assert.Equal(TefState.Confirmed, _journal.Find(approval.TransactionId)!.State);
        Assert.Equal("000001", approval.Nsu);
        Assert.Contains("Insira, aproxime ou passe o cartão", _ui.Messages);
    }

    [Fact]
    public async Task The_journal_has_the_transaction_before_the_card_is_read()
    {
        // Queda de energia com o cartão no pinpad: o diário já sabe da transação.
        var spy = new JournalSpyProvider(_journal);
        var coordinator = new TefCoordinator(spy, _journal);
        await coordinator.AuthorizeAsync("pedido-1", 1000, TefCardType.Credit, _ui);
        Assert.Equal(TefState.Started, spy.StateSeenDuringAuthorization);
    }

    [Fact]
    public async Task A_declined_card_charges_nothing()
    {
        var outcome = await Coordinator().AuthorizeAsync("pedido-1", 1051, TefCardType.Debit, _ui);
        Assert.Equal("Saldo insuficiente", Assert.IsType<TefOutcome.Declined>(outcome).Reason);
        Assert.Empty(_tef.Host);
        Assert.Empty(_journal.Pending());
    }

    [Fact]
    public async Task A_customer_who_cancels_on_the_pinpad_is_not_charged()
    {
        var outcome = await Coordinator().AuthorizeAsync("pedido-1", 1052, TefCardType.Debit, _ui);
        Assert.IsType<TefOutcome.Aborted>(outcome);
        Assert.Empty(_tef.Host);
        Assert.Empty(_journal.Pending());
    }

    [Fact]
    public async Task A_lost_answer_is_undone_on_the_spot()
    {
        // A adquirente aprovou, a resposta se perdeu: o caixa não sabe, desfaz.
        var coordinator = Coordinator();
        await Assert.ThrowsAsync<TefCommunicationException>(
            () => coordinator.AuthorizeAsync("pedido-1", 1053, TefCardType.Debit, _ui));

        var entry = Assert.Single(_tef.Host);
        Assert.Equal(HostState.Undone, entry.Value);
        Assert.Empty(_journal.Pending());
    }

    [Fact]
    public async Task A_lost_answer_with_the_network_down_is_undone_at_the_next_opening()
    {
        var coordinator = Coordinator();
        _tef.Offline = false;
        var failing = new OfflineAfterAuthorize(_tef);
        var flaky = new TefCoordinator(failing, _journal);
        await Assert.ThrowsAsync<TefCommunicationException>(
            () => flaky.AuthorizeAsync("pedido-1", 1053, TefCardType.Debit, _ui));

        var pending = Assert.Single(_journal.Pending());
        Assert.Equal(TefState.Started, pending.State);
        Assert.Equal(HostState.Authorized, _tef.Host[pending.TransactionId]);

        var recovered = Assert.Single(await coordinator.RecoverPendingAsync(_ => false));
        Assert.Equal(TefState.Undone, recovered.Resolution);
        Assert.Contains("DESFEITA", recovered.Message);
        Assert.Equal(HostState.Undone, _tef.Host[pending.TransactionId]);
    }

    [Fact]
    public async Task A_crash_after_approval_with_the_sale_saved_confirms_it()
    {
        var approval = Approval(await Coordinator().AuthorizeAsync("pedido-1", 2500, TefCardType.Credit, _ui));
        // ... a venda foi gravada, e o caixa caiu antes de confirmar.

        var reopened = new TefCoordinator(_tef, _journal);
        var recovered = Assert.Single(
            await reopened.RecoverPendingAsync(entry => entry.TransactionId == approval.TransactionId));

        Assert.Equal(TefState.Confirmed, recovered.Resolution);
        Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]);
        Assert.Contains("confirmado", recovered.Message);
    }

    [Fact]
    public async Task A_crash_after_approval_without_the_sale_undoes_it()
    {
        var approval = Approval(await Coordinator().AuthorizeAsync("pedido-1", 2500, TefCardType.Credit, _ui));
        // ... o caixa caiu antes de gravar a venda.

        var recovered = Assert.Single(await new TefCoordinator(_tef, _journal).RecoverPendingAsync(_ => false));
        Assert.Equal(TefState.Undone, recovered.Resolution);
        Assert.Equal(HostState.Undone, _tef.Host[approval.TransactionId]);
    }

    [Fact]
    public async Task No_new_card_while_a_previous_one_is_pending()
    {
        var coordinator = Coordinator();
        await coordinator.AuthorizeAsync("pedido-1", 2500, TefCardType.Credit, _ui);
        await Assert.ThrowsAsync<TefPendingException>(
            () => coordinator.AuthorizeAsync("pedido-2", 1000, TefCardType.Debit, _ui));
    }

    [Fact]
    public async Task A_confirmation_lost_to_the_network_is_retried_at_the_next_opening()
    {
        var coordinator = Coordinator();
        var approval = Approval(await coordinator.AuthorizeAsync("pedido-1", 3000, TefCardType.Debit, _ui));

        _tef.Offline = true;
        Assert.False(await coordinator.ConfirmAsync(approval));
        Assert.Equal(TefState.Approved, _journal.Find(approval.TransactionId)!.State);

        _tef.Offline = false;
        var recovered = Assert.Single(await coordinator.RecoverPendingAsync(_ => true));
        Assert.Equal(TefState.Confirmed, recovered.Resolution);
        Assert.Equal(HostState.Confirmed, _tef.Host[approval.TransactionId]);
    }

    [Fact]
    public async Task A_sale_that_fails_to_save_is_undone()
    {
        var coordinator = Coordinator();
        var approval = Approval(await coordinator.AuthorizeAsync("pedido-1", 3000, TefCardType.Debit, _ui));
        Assert.True(await coordinator.UndoAsync(approval, "erro ao gravar a venda"));
        Assert.Equal(HostState.Undone, _tef.Host[approval.TransactionId]);
        Assert.Empty(_journal.Pending());
    }

    [Fact]
    public async Task Recovery_with_the_tef_still_offline_keeps_it_pending()
    {
        var coordinator = Coordinator();
        await coordinator.AuthorizeAsync("pedido-1", 3000, TefCardType.Debit, _ui);
        _tef.Offline = true;

        var recovered = Assert.Single(await coordinator.RecoverPendingAsync(_ => false));
        Assert.Equal(TefState.Approved, recovered.Resolution);
        Assert.True(coordinator.HasPending);
    }

    [Fact]
    public async Task A_zero_amount_never_reaches_the_tef() =>
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(
            () => Coordinator().AuthorizeAsync("pedido-1", 0, TefCardType.Debit, _ui));

    private sealed class RecordingInteraction : ITefInteraction
    {
        public List<string> Messages { get; } = [];

        public void Show(string message) => Messages.Add(message);

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) =>
            Task.FromResult<string?>("");
    }

    private sealed class JournalSpyProvider(ITefJournal journal) : ITefProvider
    {
        public TefState? StateSeenDuringAuthorization { get; private set; }

        public string Name => "espião";

        public Task<TefOutcome> AuthorizeAsync(TefRequest request, ITefInteraction ui, CancellationToken cancellationToken)
        {
            StateSeenDuringAuthorization = journal.Find(request.TransactionId)?.State;
            return Task.FromResult<TefOutcome>(new TefOutcome.Declined("teste"));
        }

        public Task ConfirmAsync(TefReference reference, CancellationToken cancellationToken) => Task.CompletedTask;

        public Task UndoAsync(TefReference reference, CancellationToken cancellationToken) => Task.CompletedTask;
    }

    /// <summary>Aprova no host e cai a rede antes da resposta e do desfazimento.</summary>
    private sealed class OfflineAfterAuthorize(TefSimulator inner) : ITefProvider
    {
        public string Name => inner.Name;

        public async Task<TefOutcome> AuthorizeAsync(TefRequest request, ITefInteraction ui, CancellationToken cancellationToken)
        {
            try
            {
                return await inner.AuthorizeAsync(request, ui, cancellationToken);
            }
            finally
            {
                inner.Offline = true;
            }
        }

        public Task ConfirmAsync(TefReference reference, CancellationToken cancellationToken) =>
            inner.ConfirmAsync(reference, cancellationToken);

        public async Task UndoAsync(TefReference reference, CancellationToken cancellationToken)
        {
            try
            {
                await inner.UndoAsync(reference, cancellationToken);
            }
            finally
            {
                inner.Offline = false;  // volta a tempo da próxima abertura
            }
        }
    }
}
