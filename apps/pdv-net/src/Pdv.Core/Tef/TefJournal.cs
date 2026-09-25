namespace Pdv.Core.Tef;

public enum TefState
{
    /// <summary>Gravado ANTES de chamar o TEF. Sem resposta ainda: resultado desconhecido.</summary>
    Started,

    /// <summary>A adquirente aprovou; falta confirmar (ou desfazer).</summary>
    Approved,

    Confirmed,
    Undone,
    Declined,
    Aborted,
}

public sealed record TefJournalEntry(
    string TransactionId,
    string OrderId,
    long AmountCents,
    TefCardType CardType,
    TefState State,
    string StartedAt,
    TefApproval? Approval = null,
    string? Detail = null,
    string? UpdatedAt = null)
{
    /// <summary>Started e Approved: a adquirente pode estar segurando dinheiro do cliente.</summary>
    public bool IsPending => State is TefState.Started or TefState.Approved;

    public TefReference Reference =>
        new(TransactionId, AmountCents, Approval?.Nsu, Approval?.ProviderReference);
}

/// <summary>Diário das transações TEF — a memória que sobrevive a uma queda de energia.</summary>
/// <remarks>
/// A implementação de produção grava no mesmo SQLite do caixa, com
/// <c>synchronous=FULL</c>: o registro precisa estar no disco ANTES de o
/// cartão ser lido. Um diário que perde a última linha na queda deixa uma
/// autorização sem dono — o cliente paga e a venda não existe.
/// </remarks>
public interface ITefJournal
{
    void Record(TefJournalEntry entry);

    void Update(TefJournalEntry entry);

    TefJournalEntry? Find(string transactionId);

    IReadOnlyList<TefJournalEntry> Pending();
}

public sealed class InMemoryTefJournal : ITefJournal
{
    private readonly Dictionary<string, TefJournalEntry> _entries = new(StringComparer.Ordinal);
    private readonly Lock _gate = new();

    public void Record(TefJournalEntry entry)
    {
        lock (_gate)
        {
            if (!_entries.TryAdd(entry.TransactionId, entry))
            {
                throw new InvalidOperationException($"Transação TEF {entry.TransactionId} já registrada.");
            }
        }
    }

    public void Update(TefJournalEntry entry)
    {
        lock (_gate)
        {
            if (!_entries.ContainsKey(entry.TransactionId))
            {
                throw new InvalidOperationException($"Transação TEF {entry.TransactionId} desconhecida.");
            }
            _entries[entry.TransactionId] = entry;
        }
    }

    public TefJournalEntry? Find(string transactionId)
    {
        lock (_gate)
        {
            return _entries.GetValueOrDefault(transactionId);
        }
    }

    public IReadOnlyList<TefJournalEntry> Pending()
    {
        lock (_gate)
        {
            return _entries.Values.Where(e => e.IsPending).OrderBy(e => e.StartedAt, StringComparer.Ordinal).ToList();
        }
    }
}
