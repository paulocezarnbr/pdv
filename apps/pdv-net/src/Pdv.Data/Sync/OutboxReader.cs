using System.Text.Json;
using Pdv.Core;

namespace Pdv.Data.Sync;

/// <summary>
/// Leitura e baixa da fila de saída — a metade cliente de "zero duplicidade,
/// zero perda" (o <c>OutboxReader</c> do Python, regra por regra).
/// </summary>
/// <remarks>
/// <list type="bullet">
/// <item>Nada sai da fila sem o veredito da nuvem.</item>
/// <item>Lotes em ordem de <c>seq</c>: a nuvem valida a cadeia de auditoria, e um elo fora de ordem seria recusado.</item>
/// <item>Backoff exponencial até 5 minutos: servidor fora do ar não vira tempestade.</item>
/// </list>
/// </remarks>
public sealed class OutboxReader(PdvDatabase database, TimeProvider? clock = null)
{
    /// <summary>
    /// Tabelas que podem ser marcadas como sincronizadas. Lista fechada: o nome
    /// vem do banco e é interpolado no SQL — uma linha adulterada no outbox não
    /// pode virar injeção.
    /// </summary>
    public static readonly IReadOnlySet<string> SyncableTables = new HashSet<string>(StringComparer.Ordinal)
    {
        "orders", "order_items", "order_item_ingredients", "payments", "stock_movements", "audit_ledger",
        "cash_sessions", "customers", "cashback_ledger", "prepaid_ledger", "credit_account_ledger",
        "customer_credit_accounts", "discount_tiers", "customer_discount_tiers",
    };

    /// <summary>Teto do backoff: além disso o caixa ficaria mudo tempo demais.</summary>
    public const int MaxBackoffSeconds = 300;

    /// <summary>Depois de N tentativas o item vira quarentena: sai do caminho, nunca é apagado.</summary>
    public const int MaxAttempts = 25;

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    public static int BackoffSeconds(int attempts) =>
        attempts >= 9 ? MaxBackoffSeconds : Math.Min(1 << attempts, MaxBackoffSeconds);

    /// <summary>
    /// O próximo lote elegível. Não marca nada como "em voo": se o processo
    /// morrer no envio, o item segue elegível — reenviar é seguro, perder não.
    /// </summary>
    public IReadOnlyList<OutboxItem> ClaimBatch(int limit = 200)
    {
        using var command = database.Connection.CreateCommand();
        command.CommandText =
            "SELECT seq, entity_table, entity_id, client_uuid, operation, payload_json, attempts " +
            "FROM sync_outbox WHERE available_at <= $now AND attempts < $max ORDER BY seq LIMIT $limit";
        command.Parameters.AddWithValue("$now", Iso.Now(_clock));
        command.Parameters.AddWithValue("$max", MaxAttempts);
        command.Parameters.AddWithValue("$limit", limit);
        using var reader = command.ExecuteReader();
        var items = new List<OutboxItem>();
        while (reader.Read())
        {
            using var payload = JsonDocument.Parse(reader.GetString(5));
            items.Add(new OutboxItem(
                reader.GetInt64(0), reader.GetString(1), reader.GetString(2), reader.GetString(3),
                reader.GetString(4), payload.RootElement.Clone(), reader.GetInt32(6)));
        }
        return items;
    }

    public long PendingCount() => Convert.ToInt64(database.Scalar("SELECT COUNT(*) FROM sync_outbox"));

    public long DeadLetterCount() =>
        Convert.ToInt64(database.Scalar("SELECT COUNT(*) FROM sync_outbox WHERE attempts >= $max", ("$max", MaxAttempts)));

    /// <summary>Fila viva, quarentena, o item vivo mais antigo e o último motivo de quarentena.</summary>
    public (long Live, long Dead, string? Oldest, string? Reason) Health()
    {
        long live;
        string? oldest;
        using (var command = database.Connection.CreateCommand())
        {
            command.CommandText = "SELECT COUNT(*), MIN(created_at) FROM sync_outbox WHERE attempts < $max";
            command.Parameters.AddWithValue("$max", MaxAttempts);
            using var reader = command.ExecuteReader();
            reader.Read();
            live = reader.GetInt64(0);
            oldest = reader.IsDBNull(1) ? null : reader.GetString(1);
        }
        var dead = DeadLetterCount();
        var reason = database.Scalar(
            "SELECT last_error FROM sync_outbox WHERE attempts >= $max ORDER BY available_at DESC, seq DESC LIMIT 1",
            ("$max", MaxAttempts)) as string;
        return (live, dead, string.IsNullOrEmpty(oldest) ? null : oldest, string.IsNullOrEmpty(reason) ? null : reason);
    }

    /// <summary>
    /// Tira da fila o que a nuvem confirmou e marca a entidade sincronizada —
    /// na mesma transação: nem entidade marcada com item na fila (reenvio
    /// eterno), nem item fora da fila sem a entidade marcada (rastro perdido).
    /// </summary>
    public int Settle(IReadOnlyList<OutboxItem> items, IReadOnlyDictionary<string, ItemAck> acks)
    {
        var settledAt = Iso.Now(_clock);
        return database.InTransaction(transaction =>
        {
            var settled = 0;
            foreach (var item in items)
            {
                if (!acks.TryGetValue(item.ClientUuid, out var ack) || !ack.Status.IsSettled()) continue;
                if (SyncableTables.Contains(item.EntityTable))
                {
                    // Seguro: o nome foi conferido contra a lista fechada.
                    using var mark = transaction.Command(
                        $"UPDATE {item.EntityTable} SET is_synced = 1, synced_at = $at WHERE client_uuid = $uuid",
                        ("$at", settledAt), ("$uuid", item.ClientUuid));
                    mark.ExecuteNonQuery();
                }
                using var delete = transaction.Command("DELETE FROM sync_outbox WHERE seq = $seq", ("$seq", item.Seq));
                delete.ExecuteNonQuery();
                settled++;
            }
            return settled;
        });
    }

    /// <summary>Devolve os itens à fila com backoff exponencial.</summary>
    public void Defer(IEnumerable<OutboxItem> items, string error)
    {
        var now = _clock.GetUtcNow();
        database.InTransaction(transaction =>
        {
            foreach (var item in items)
            {
                var attempts = item.Attempts + 1;
                using var command = transaction.Command(
                    "UPDATE sync_outbox SET attempts = $attempts, last_error = $error, available_at = $at WHERE seq = $seq",
                    ("$attempts", attempts), ("$error", Truncate(error)),
                    ("$at", Iso.Format(now.AddSeconds(BackoffSeconds(attempts)))), ("$seq", item.Seq));
                command.ExecuteNonQuery();
            }
        });
    }

    /// <summary>
    /// Rejeitado vai para a quarentena: o mesmo payload teria o mesmo veredito.
    /// Nada é apagado — o item fica com <c>attempts</c> no teto, esperando gente.
    /// </summary>
    public void Quarantine(IEnumerable<OutboxItem> items, string error)
    {
        var now = Iso.Now(_clock);
        database.InTransaction(transaction =>
        {
            foreach (var item in items)
            {
                using var command = transaction.Command(
                    "UPDATE sync_outbox SET attempts = $attempts, last_error = $error, available_at = $at WHERE seq = $seq",
                    ("$attempts", MaxAttempts), ("$error", Truncate(error)), ("$at", now), ("$seq", item.Seq));
                command.ExecuteNonQuery();
            }
        });
    }

    /// <summary>Os primeiros 500 caracteres, como o <c>error[:500]</c> do Python (por code point).</summary>
    private static string Truncate(string error)
    {
        var points = error.EnumerateRunes().Take(500).ToArray();
        return string.Concat(points.Select(rune => rune.ToString()));
    }
}

/// <summary>Cursores do pull incremental, por tabela.</summary>
public sealed class CursorStore(PdvDatabase database, TimeProvider? clock = null)
{
    public long Get(string entityTable) =>
        database.Scalar("SELECT last_server_seq FROM sync_cursors WHERE entity_table = $t", ("$t", entityTable)) is { } value
            ? Convert.ToInt64(value)
            : 0;

    public void Set(string entityTable, long serverSeq)
    {
        var now = Iso.Now(clock);
        database.InTransaction(transaction =>
        {
            using var command = transaction.Command(
                "INSERT INTO sync_cursors (entity_table, last_server_seq, last_pulled_at) VALUES ($t, $seq, $now) " +
                "ON CONFLICT (entity_table) DO UPDATE SET last_server_seq = excluded.last_server_seq, " +
                "last_pulled_at = excluded.last_pulled_at",
                ("$t", entityTable), ("$seq", serverSeq), ("$now", now));
            command.ExecuteNonQuery();
        });
    }
}
