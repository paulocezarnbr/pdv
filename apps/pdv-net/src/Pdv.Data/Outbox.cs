using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Audit;

namespace Pdv.Data;

/// <summary>A fila de saída para a nuvem — o coração da garantia offline.</summary>
/// <remarks>
/// Sempre dentro da transação da venda: não existe venda gravada que não suba,
/// nem item na fila de uma venda que não existe.
/// </remarks>
public sealed class Outbox(TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;

    public void Enqueue(
        SqliteTransaction transaction,
        string entityTable,
        string entityId,
        string clientUuid,
        string operation,
        IDictionary<string, object?> payload)
    {
        if (operation is not ("insert" or "update" or "delete"))
        {
            throw new ArgumentException($"Operação de outbox inválida: {operation}", nameof(operation));
        }

        var now = Iso.Now(_clock);
        using var command = transaction.Command(
            """
            INSERT INTO sync_outbox
                (entity_table, entity_id, client_uuid, operation, payload_json, available_at, created_at)
            VALUES ($table, $id, $uuid, $operation, $payload, $now, $now)
            """,
            ("$table", entityTable),
            ("$id", entityId),
            ("$uuid", clientUuid),
            ("$operation", operation),
            ("$payload", CanonicalJson.SerializeForOutbox(payload)),
            ("$now", now));
        command.ExecuteNonQuery();
    }

    public static long PendingCount(SqliteConnection connection)
    {
        using var command = Sql.Command(connection, null, "SELECT COUNT(*) FROM sync_outbox");
        return Convert.ToInt64(command.ExecuteScalar());
    }
}
