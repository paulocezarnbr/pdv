using System.Text.Json;
using Microsoft.Data.Sqlite;
using Pdv.Core.Tef;

namespace Pdv.Data;

/// <summary>O diário do TEF no banco do caixa.</summary>
/// <remarks>
/// <para>
/// Conexão PRÓPRIA, separada da conexão da venda, e cada gravação é um commit
/// isolado com <c>synchronous=FULL</c>. Se o diário escrevesse dentro da
/// transação da venda, um rollback da venda apagaria o registro de um cartão
/// que já foi lido — exatamente a pendência que ele existe para lembrar.
/// </para>
/// <para>
/// A tabela é do C# e entra com <c>CREATE TABLE IF NOT EXISTS</c>, sem mexer
/// no <c>user_version</c>: o PDV em Python a ignora (ver docs/port_csharp.md).
/// </para>
/// </remarks>
public sealed class SqliteTefJournal : ITefJournal, IDisposable
{
    private static readonly JsonSerializerOptions Json = new() { WriteIndented = false };

    private readonly PdvDatabase _database;

    public SqliteTefJournal(string databasePath)
    {
        _database = new PdvDatabase(databasePath);
        _database.Execute(
            """
            CREATE TABLE IF NOT EXISTS tef_transactions (
                transaction_id TEXT PRIMARY KEY,
                order_id       TEXT NOT NULL,
                amount_cents   INTEGER NOT NULL CHECK (amount_cents > 0),
                card_type      TEXT NOT NULL,
                state          TEXT NOT NULL,
                started_at     TEXT NOT NULL,
                updated_at     TEXT,
                detail         TEXT,
                approval_json  TEXT
            )
            """);
        _database.Execute(
            "CREATE INDEX IF NOT EXISTS idx_tef_transactions_pending ON tef_transactions (state) " +
            "WHERE state IN ('Started', 'Approved')");
    }

    public void Record(TefJournalEntry entry)
    {
        try
        {
            _database.Execute(
                """
                INSERT INTO tef_transactions
                    (transaction_id, order_id, amount_cents, card_type, state, started_at, updated_at, detail, approval_json)
                VALUES ($id, $order, $amount, $card, $state, $started, $updated, $detail, $approval)
                """,
                Parameters(entry));
        }
        catch (SqliteException error) when (error.SqliteErrorCode == 19)
        {
            throw new InvalidOperationException($"Transação TEF {entry.TransactionId} já registrada.");
        }
    }

    public void Update(TefJournalEntry entry)
    {
        var changed = _database.Execute(
            """
            UPDATE tef_transactions
               SET state = $state, updated_at = $updated, detail = $detail, approval_json = $approval
             WHERE transaction_id = $id
            """,
            Parameters(entry));
        if (changed != 1)
        {
            throw new InvalidOperationException($"Transação TEF {entry.TransactionId} desconhecida.");
        }
    }

    public TefJournalEntry? Find(string transactionId) =>
        Query("WHERE transaction_id = $id", ("$id", transactionId)).SingleOrDefault();

    public IReadOnlyList<TefJournalEntry> Pending() =>
        Query("WHERE state IN ('Started', 'Approved') ORDER BY started_at");

    private List<TefJournalEntry> Query(string where, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(
            _database.Connection, null,
            "SELECT transaction_id, order_id, amount_cents, card_type, state, started_at, updated_at, detail, approval_json " +
            "FROM tef_transactions " + where,
            parameters);
        using var reader = command.ExecuteReader();
        var entries = new List<TefJournalEntry>();
        while (reader.Read())
        {
            entries.Add(new TefJournalEntry(
                TransactionId: reader.GetString(0),
                OrderId: reader.GetString(1),
                AmountCents: reader.GetInt64(2),
                CardType: Enum.Parse<TefCardType>(reader.GetString(3)),
                State: Enum.Parse<TefState>(reader.GetString(4)),
                StartedAt: reader.GetString(5),
                Approval: reader.IsDBNull(8) ? null : JsonSerializer.Deserialize<TefApproval>(reader.GetString(8), Json),
                Detail: reader.IsDBNull(7) ? null : reader.GetString(7),
                UpdatedAt: reader.IsDBNull(6) ? null : reader.GetString(6)));
        }
        return entries;
    }

    private static (string, object?)[] Parameters(TefJournalEntry entry) =>
    [
        ("$id", entry.TransactionId),
        ("$order", entry.OrderId),
        ("$amount", entry.AmountCents),
        ("$card", entry.CardType.ToString()),
        ("$state", entry.State.ToString()),
        ("$started", entry.StartedAt),
        ("$updated", entry.UpdatedAt),
        ("$detail", entry.Detail),
        ("$approval", entry.Approval is null ? null : JsonSerializer.Serialize(entry.Approval, Json)),
    ];

    public void Dispose() => _database.Dispose();
}
