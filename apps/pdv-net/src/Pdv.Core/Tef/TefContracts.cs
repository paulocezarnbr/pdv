namespace Pdv.Core.Tef;

public enum TefCardType
{
    Debit,
    Credit,
    Pix,
    Voucher,
}

/// <summary>O que o caixa pede ao TEF.</summary>
/// <param name="TransactionId">Gerado pelo PDV antes de chamar o TEF; é a chave do desfazimento.</param>
public sealed record TefRequest(
    string TransactionId,
    string OrderId,
    long AmountCents,
    TefCardType CardType,
    int Installments = 1);

/// <summary>Transação aprovada pela adquirente — ainda NÃO confirmada.</summary>
/// <remarks>
/// Aprovada não é concluída: até a confirmação, a adquirente trata a
/// transação como pendente e a desfaz se o PDV não confirmar. É isso que
/// protege o cliente de pagar por uma venda que não foi registrada.
/// </remarks>
public sealed record TefApproval(
    string TransactionId,
    long AmountCents,
    TefCardType CardType,
    string Nsu,
    string AuthorizationCode,
    string Acquirer,
    string CardBrand,
    string CustomerReceipt,
    string MerchantReceipt,
    string? ProviderReference = null);

public abstract record TefOutcome
{
    private TefOutcome() { }

    public sealed record Approved(TefApproval Approval) : TefOutcome;

    /// <summary>Negada pela adquirente ou pelo emissor (saldo, senha, cartão).</summary>
    public sealed record Declined(string Reason) : TefOutcome;

    /// <summary>Interrompida no balcão (operador ou cliente cancelou no pinpad).</summary>
    public sealed record Aborted(string Reason) : TefOutcome;
}

/// <summary>Como o TEF fala com o operador durante a transação.</summary>
/// <remarks>
/// Toda solução de TEF conduz a transação por mensagens e perguntas ("Insira o
/// cartão", "Crédito ou débito?", "Número de parcelas"). A interface do caixa
/// implementa isto; o provedor não sabe se está numa janela WPF ou num teste.
/// </remarks>
public interface ITefInteraction
{
    void Show(string message);

    /// <returns>O índice escolhido, ou <c>null</c> se o operador cancelou.</returns>
    Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken);

    /// <returns>O texto digitado, ou <c>null</c> se o operador cancelou.</returns>
    Task<string?> AskAsync(string prompt, CancellationToken cancellationToken);
}

/// <summary>Referência para desfazer ou confirmar, com ou sem resposta da adquirente.</summary>
public sealed record TefReference(string TransactionId, long AmountCents, string? Nsu, string? ProviderReference);

/// <summary>A solução de TEF (SiTef, PayGo, TEF Dial, a da adquirente...).</summary>
/// <remarks>
/// Cada provedor real entra como uma implementação desta interface; o resto do
/// PDV — pendência, desfazimento, recuperação depois de queda — mora no
/// <see cref="TefCoordinator"/> e vale para todos.
/// </remarks>
public interface ITefProvider
{
    string Name { get; }

    Task<TefOutcome> AuthorizeAsync(TefRequest request, ITefInteraction ui, CancellationToken cancellationToken);

    /// <summary>A venda foi registrada: a adquirente pode liquidar.</summary>
    Task ConfirmAsync(TefReference reference, CancellationToken cancellationToken);

    /// <summary>Desfazimento: a venda não se concretizou, a adquirente estorna a autorização.</summary>
    /// <remarks>Precisa ser idempotente e aceitar transação desconhecida (nunca chegou ao host).</remarks>
    Task UndoAsync(TefReference reference, CancellationToken cancellationToken);
}

/// <summary>Falha de comunicação: não se sabe se a adquirente aprovou.</summary>
public sealed class TefCommunicationException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>Há transação pendente de uma venda anterior; resolva antes de iniciar outra.</summary>
public sealed class TefPendingException(string message) : Exception(message);
