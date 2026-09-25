namespace Pdv.Core.Tef;

/// <summary>Resultado da recuperação de uma pendência, para mostrar ao operador.</summary>
public sealed record TefRecovery(TefJournalEntry Entry, TefState Resolution, string Message);

/// <summary>
/// O ciclo de vida da transação TEF, igual para qualquer provedor.
/// </summary>
/// <remarks>
/// <para>A regra que protege o cliente e a loja:</para>
/// <list type="number">
///   <item>o diário registra a transação ANTES de o TEF ser chamado;</item>
///   <item>aprovada, ela fica pendente até a venda ser gravada;</item>
///   <item>venda gravada → confirmar; venda não gravada → desfazer;</item>
///   <item>
///     depois de uma queda, a abertura do caixa resolve as pendências antes de
///     qualquer venda nova: aprovada com venda gravada é confirmada; o resto é
///     desfeito.
///   </item>
/// </list>
/// <para>
/// Sem isto, os dois erros clássicos: o cliente paga e a venda não existe
/// (queda entre aprovar e gravar), ou a venda existe e a adquirente estorna
/// sozinha por falta de confirmação — a loja entrega e não recebe.
/// </para>
/// </remarks>
public sealed class TefCoordinator(ITefProvider provider, ITefJournal journal, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    public string ProviderName => provider.Name;

    public bool HasPending => journal.Pending().Count > 0;

    /// <summary>Pede a autorização. A aprovação devolvida AINDA precisa de <see cref="ConfirmAsync"/>.</summary>
    /// <exception cref="TefPendingException">Há pendência de venda anterior não resolvida.</exception>
    /// <exception cref="TefCommunicationException">Resultado desconhecido; a transação foi (ou será) desfeita.</exception>
    public async Task<TefOutcome> AuthorizeAsync(
        string orderId, long amountCents, TefCardType cardType, ITefInteraction ui,
        int installments = 1, CancellationToken cancellationToken = default)
    {
        if (amountCents <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(amountCents), "Valor do TEF precisa ser positivo.");
        }
        if (HasPending)
        {
            throw new TefPendingException(
                "Há uma transação de cartão pendente de uma venda anterior. " +
                "Resolva as pendências antes de passar outro cartão.");
        }

        var request = new TefRequest(Iso.NewId(), orderId, amountCents, cardType, installments);
        var entry = new TefJournalEntry(
            request.TransactionId, orderId, amountCents, cardType, TefState.Started, Iso.Now(_clock));
        journal.Record(entry);

        TefOutcome outcome;
        try
        {
            outcome = await provider.AuthorizeAsync(request, ui, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            // Não se sabe se a adquirente aprovou. Desfazer é sempre seguro: se
            // ela nunca viu a transação, o desfazimento é inócuo.
            await TryUndoAsync(entry, "falha de comunicação durante a autorização", cancellationToken)
                .ConfigureAwait(false);
            throw new TefCommunicationException(
                "A comunicação com o TEF falhou no meio da transação. Ela foi desfeita — " +
                "se o cliente viu débito, ele será estornado. Passe o cartão de novo.", error);
        }

        switch (outcome)
        {
            case TefOutcome.Approved approved:
                journal.Update(entry with { State = TefState.Approved, Approval = approved.Approval, UpdatedAt = Iso.Now(_clock) });
                break;
            case TefOutcome.Declined declined:
                journal.Update(entry with { State = TefState.Declined, Detail = declined.Reason, UpdatedAt = Iso.Now(_clock) });
                break;
            case TefOutcome.Aborted aborted:
                journal.Update(entry with { State = TefState.Aborted, Detail = aborted.Reason, UpdatedAt = Iso.Now(_clock) });
                break;
        }
        return outcome;
    }

    /// <summary>A venda foi gravada: confirma na adquirente.</summary>
    /// <remarks>
    /// Se a confirmação não chegar (rede caiu), a transação continua pendente e
    /// a próxima <see cref="RecoverPendingAsync"/> a confirma — a venda existe.
    /// </remarks>
    public async Task<bool> ConfirmAsync(TefApproval approval, CancellationToken cancellationToken = default)
    {
        var entry = Require(approval.TransactionId, TefState.Approved);
        try
        {
            await provider.ConfirmAsync(entry.Reference, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            journal.Update(entry with { Detail = $"confirmação pendente: {error.Message}", UpdatedAt = Iso.Now(_clock) });
            return false;
        }
        journal.Update(entry with { State = TefState.Confirmed, UpdatedAt = Iso.Now(_clock) });
        return true;
    }

    /// <summary>A venda não se concretizou (erro ao gravar, cupom, desistência): desfaz.</summary>
    public async Task<bool> UndoAsync(TefApproval approval, string reason, CancellationToken cancellationToken = default)
    {
        var entry = Require(approval.TransactionId, TefState.Approved);
        return await TryUndoAsync(entry, reason, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Resolve o que ficou pendente — na abertura do caixa, antes da primeira venda.
    /// </summary>
    /// <param name="saleWasRecorded">
    /// Se a venda daquela transação chegou ao banco (pagamento gravado com o
    /// <c>TransactionId</c>). É a única fonte de verdade: o que está no banco foi
    /// vendido; o que não está, não foi.
    /// </param>
    public async Task<IReadOnlyList<TefRecovery>> RecoverPendingAsync(
        Func<TefJournalEntry, bool> saleWasRecorded, CancellationToken cancellationToken = default)
    {
        var results = new List<TefRecovery>();
        foreach (var entry in journal.Pending())
        {
            var confirm = entry.State == TefState.Approved && saleWasRecorded(entry);
            if (confirm)
            {
                var confirmed = await ConfirmAsync(entry.Approval!, cancellationToken).ConfigureAwait(false);
                results.Add(new TefRecovery(
                    journal.Find(entry.TransactionId)!,
                    confirmed ? TefState.Confirmed : TefState.Approved,
                    confirmed
                        ? $"Cartão de {Money(entry.AmountCents)} (NSU {entry.Approval!.Nsu}) confirmado: a venda estava gravada."
                        : $"Cartão de {Money(entry.AmountCents)} ainda sem confirmação: sem comunicação com o TEF."));
                continue;
            }

            var undone = await TryUndoAsync(entry, "pendência resolvida na abertura", cancellationToken).ConfigureAwait(false);
            results.Add(new TefRecovery(
                journal.Find(entry.TransactionId)!,
                undone ? TefState.Undone : entry.State,
                undone
                    ? $"Transação de cartão de {Money(entry.AmountCents)} foi DESFEITA: a venda não chegou a ser gravada. " +
                      "Se o cliente foi cobrado, o valor será estornado. Retenha o comprovante."
                    : $"Transação de cartão de {Money(entry.AmountCents)} ainda pendente: sem comunicação com o TEF."));
        }
        return results;
    }

    private async Task<bool> TryUndoAsync(TefJournalEntry entry, string reason, CancellationToken cancellationToken)
    {
        try
        {
            await provider.UndoAsync(entry.Reference, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception error) when (error is not OperationCanceledException)
        {
            // Fica pendente: a próxima abertura tenta de novo.
            journal.Update(entry with { Detail = $"desfazimento pendente ({reason}): {error.Message}", UpdatedAt = Iso.Now(_clock) });
            return false;
        }
        journal.Update(entry with { State = TefState.Undone, Detail = reason, UpdatedAt = Iso.Now(_clock) });
        return true;
    }

    private TefJournalEntry Require(string transactionId, TefState state)
    {
        var entry = journal.Find(transactionId)
            ?? throw new InvalidOperationException($"Transação TEF {transactionId} não está no diário.");
        if (entry.State != state)
        {
            throw new InvalidOperationException(
                $"Transação TEF {transactionId} está {entry.State}, esperado {state}.");
        }
        return entry;
    }

    private static string Money(long cents) =>
        (cents / 100m).ToString("C", System.Globalization.CultureInfo.GetCultureInfo("pt-BR"));
}
