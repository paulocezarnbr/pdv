using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Audit;

namespace Pdv.Data;

/// <summary>
/// Grava e verifica o <c>audit_ledger</c> — o mesmo que o <c>AuditService</c> do Python.
/// </summary>
/// <remarks>
/// Escreve na transação da operação auditada (não existe venda sem trilha nem
/// trilha sem venda) e enfileira o elo no outbox com o mesmo payload do Python:
/// a nuvem verifica a cadeia de novo, sem confiar no terminal.
/// </remarks>
public sealed class AuditLedger
{
    private readonly string _tenantId;
    private readonly string _storeId;
    private readonly string _deviceId;
    private readonly byte[] _secret;
    private readonly Outbox _outbox;
    private readonly TimeProvider _clock;

    public AuditLedger(string tenantId, string storeId, string deviceId, byte[] deviceSecret, TimeProvider? clock = null)
    {
        if (deviceSecret.Length == 0)
        {
            throw new ArgumentException("device_secret vazio: a cadeia seria forjável.", nameof(deviceSecret));
        }
        _tenantId = tenantId;
        _storeId = storeId;
        _deviceId = deviceId;
        _secret = deviceSecret;
        _clock = clock ?? TimeProvider.System;
        _outbox = new Outbox(_clock);
    }

    /// <summary>Acrescenta um elo. Nunca atualiza nada existente.</summary>
    public LedgerLink Append(
        SqliteTransaction transaction,
        string eventType,
        string actorUserId,
        IDictionary<string, object?> payload,
        string severity = "info",
        string? authorizerUserId = null)
    {
        string prevHash;
        using (var last = transaction.Command(
                   "SELECT hash FROM audit_ledger WHERE device_id = $device ORDER BY seq DESC LIMIT 1",
                   ("$device", _deviceId)))
        {
            prevHash = last.ExecuteScalar() as string ?? AuditChain.GenesisHash;
        }

        long seq;
        using (var next = transaction.Command(
                   "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_ledger WHERE device_id = $device",
                   ("$device", _deviceId)))
        {
            seq = Convert.ToInt64(next.ExecuteScalar());
        }

        var createdAt = Iso.Now(_clock);
        var payloadJson = CanonicalJson.Serialize(payload);
        var hash = AuditChain.ComputeHash(_secret, prevHash, seq, eventType, payloadJson, createdAt);
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();

        using (var insert = transaction.Command(
                   """
                   INSERT INTO audit_ledger
                       (id, tenant_id, store_id, device_id, seq, event_type, severity, actor_user_id,
                        authorizer_user_id, payload_json, prev_hash, hash, created_at, client_uuid, is_synced)
                   VALUES ($id, $tenant, $store, $device, $seq, $event, $severity, $actor,
                           $authorizer, $payload, $prev, $hash, $created, $uuid, 0)
                   """,
                   ("$id", id), ("$tenant", _tenantId), ("$store", _storeId), ("$device", _deviceId),
                   ("$seq", seq), ("$event", eventType), ("$severity", severity), ("$actor", actorUserId),
                   ("$authorizer", authorizerUserId), ("$payload", payloadJson), ("$prev", prevHash),
                   ("$hash", hash), ("$created", createdAt), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }

        _outbox.Enqueue(transaction, "audit_ledger", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["tenant_id"] = _tenantId,
            ["store_id"] = _storeId,
            ["device_id"] = _deviceId,
            ["seq"] = seq,
            ["event_type"] = eventType,
            ["severity"] = severity,
            ["actor_user_id"] = actorUserId,
            ["authorizer_user_id"] = authorizerUserId,
            ["payload_json"] = payloadJson,
            ["prev_hash"] = prevHash,
            ["hash"] = hash,
            ["created_at"] = createdAt,
            ["client_uuid"] = clientUuid,
        });

        return new LedgerLink(seq, eventType, payloadJson, prevHash, hash, createdAt);
    }

    /// <summary>Revalida a cadeia inteira deste terminal. Roda na abertura do caixa.</summary>
    /// <exception cref="AuditChainException">Buraco na sequência ou hash divergente.</exception>
    public void Verify(SqliteConnection connection)
    {
        using var command = Sql.Command(
            connection, null,
            "SELECT seq, event_type, payload_json, prev_hash, hash, created_at " +
            "FROM audit_ledger WHERE device_id = $device ORDER BY seq",
            ("$device", _deviceId));
        using var reader = command.ExecuteReader();
        var links = new List<LedgerLink>();
        while (reader.Read())
        {
            links.Add(new LedgerLink(
                reader.GetInt64(0), reader.GetString(1), reader.GetString(2),
                reader.GetString(3), reader.GetString(4), reader.GetString(5)));
        }
        AuditChain.Verify(links, _secret);
    }
}
