using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Pdv.Data.Sync;

/// <summary>Raiz das falhas de sincronização.</summary>
public class SyncException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>
/// Falha de rede ou de servidor: sempre retentável, o lote volta à fila.
/// "Não sei se chegou" — e a resposta a isso é reenviar.
/// </summary>
public sealed class TransportException(string message, Exception? inner = null) : SyncException(message, inner);

/// <summary>Credencial do terminal inválida ou revogada (401/403). Reenviar não resolve.</summary>
public sealed class AuthException(string message) : SyncException(message);

/// <summary>O veredito da nuvem para cada item do lote.</summary>
public enum ItemStatus
{
    /// <summary>Gravado agora.</summary>
    Applied,

    /// <summary>Já existia: reenvio depois de uma resposta perdida. Também é sucesso.</summary>
    Duplicate,

    /// <summary>Recusado: payload inválido, cadeia quebrada. Reenviar dá o mesmo.</summary>
    Rejected,
}

public static class ItemStatuses
{
    /// <summary>
    /// Status desconhecido é recusa, nunca sucesso: uma nuvem mais nova com um
    /// status novo não pode fazer este caixa apagar da fila o que talvez não chegou.
    /// </summary>
    public static ItemStatus Parse(string? value) => value switch
    {
        "applied" => ItemStatus.Applied,
        "duplicate" => ItemStatus.Duplicate,
        _ => ItemStatus.Rejected,
    };

    public static bool IsSettled(this ItemStatus status) => status is ItemStatus.Applied or ItemStatus.Duplicate;

    public static string Wire(this ItemStatus status) => status switch
    {
        ItemStatus.Applied => "applied",
        ItemStatus.Duplicate => "duplicate",
        _ => "rejected",
    };
}

/// <summary>Uma linha da fila de saída, pronta para subir.</summary>
public sealed record OutboxItem(
    long Seq,
    string EntityTable,
    string EntityId,
    string ClientUuid,
    string Operation,
    JsonElement Payload,
    int Attempts);

/// <summary>O lote enviado à nuvem.</summary>
public sealed record PushBatch(string DeviceId, string TenantId, string StoreId, IReadOnlyList<OutboxItem> Items)
{
    /// <summary>
    /// Derivada do conteúdo, não do relógio: a retentativa depois de um timeout
    /// manda a mesma chave, e a nuvem reconhece a repetição mesmo tendo aplicado
    /// o primeiro envio.
    /// </summary>
    public string IdempotencyKey => IdempotencyKeyFor(DeviceId, Items.Select(item => item.ClientUuid));

    public static string IdempotencyKeyFor(string deviceId, IEnumerable<string> clientUuids)
    {
        var material = string.Join("|", clientUuids);
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes($"{deviceId}|{material}"));
        return Convert.ToHexStringLower(digest)[..32];
    }
}

public sealed record ItemAck(string ClientUuid, ItemStatus Status, string? Message = null, long? ServerSeq = null);

public sealed record PullRequest(string TenantId, string StoreId, string EntityTable, long SinceServerSeq, int Limit = 500);

public sealed record PullResponse(
    string EntityTable, IReadOnlyList<JsonElement> Rows, long LastServerSeq, bool HasMore = false);

/// <summary>
/// Como a fila deste terminal está, contado por ele. O relógio vai cru: quem
/// calcula o desvio é a nuvem — o caixa não é testemunha da própria hora.
/// </summary>
public sealed record TerminalHealth(
    string DeviceId,
    string TenantId,
    string TerminalClock,
    long PendingItems,
    long QuarantinedItems,
    string? OldestPendingAt,
    string? LastQuarantineReason);

public sealed record SyncReport(int Sent = 0, int Settled = 0, int Rejected = 0, int Deferred = 0, string? Error = null)
{
    public bool MadeProgress => Settled > 0;
}

public sealed record CommandCycleReport(
    int Fetched = 0, int Accepted = 0, int Applied = 0, int Refused = 0, int Reported = 0, int Awaiting = 0,
    string? Error = null);

/// <summary>
/// O canal de comandos do painel. Opcional: uma nuvem que não o fala não é
/// falha, e o caixa só deixa de receber comando.
/// </summary>
public interface ICommandTransport
{
    /// <summary>
    /// Os comandos endereçados a este terminal, <b>não verificados</b>. A
    /// entrega não consome o comando na nuvem: ele volta até ser relatado.
    /// </summary>
    Task<IReadOnlyList<Pdv.Core.Remote.RemoteCommand>> FetchCommandsAsync(
        string tenantId, string storeId, string deviceId, int limit, CancellationToken cancellation);

    /// <summary>Relata resultados e esperas. Devolve os <c>command_uuid</c> que a nuvem aceitou.</summary>
    Task<IReadOnlyList<string>> ReportCommandsAsync(
        string tenantId, string storeId, string deviceId,
        IReadOnlyList<Remote.CommandResult> results, IReadOnlyList<Remote.AwaitingNotice> awaiting,
        CancellationToken cancellation);
}

/// <summary>Canal até a nuvem. HTTP em produção, falso nos testes de falha.</summary>
public interface ISyncTransport
{
    /// <exception cref="TransportException">Rede, 5xx, timeout: retentável.</exception>
    /// <exception cref="AuthException">Credencial inválida.</exception>
    Task<IReadOnlyList<ItemAck>> PushAsync(PushBatch batch, CancellationToken cancellation);

    Task<PullResponse> PullAsync(PullRequest request, CancellationToken cancellation);

    /// <returns>O desvio do relógio calculado na nuvem, em ms.</returns>
    Task<long> HeartbeatAsync(TerminalHealth health, CancellationToken cancellation);
}
