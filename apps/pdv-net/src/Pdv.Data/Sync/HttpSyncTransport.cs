using System.Globalization;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Pdv.Data.Provisioning;

namespace Pdv.Data.Sync;

/// <summary>
/// O canal HTTP até a nuvem: <c>/sync/push</c>, <c>/sync/pull</c> e
/// <c>/devices/heartbeat</c>, com o token do terminal.
/// </summary>
/// <remarks>
/// <para>
/// Timeout curto e nenhum retry aqui dentro: quem retenta é a fila, com
/// backoff e idempotência. Retry nas duas camadas tornaria impossível saber
/// quantas vezes um lote chegou.
/// </para>
/// <para>
/// Todo erro vira <see cref="TransportException"/>, retentável; só 401/403
/// vira <see cref="AuthException"/>. Um 500 pode ter nascido depois do commit
/// — tratá-lo como definitivo perderia a venda.
/// </para>
/// </remarks>
public sealed class HttpSyncTransport : ISyncTransport, ICommandTransport, IDisposable
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(20);

    private readonly HttpClient _client;
    private readonly bool _ownsClient;
    private readonly string _root;

    public HttpSyncTransport(string baseUrl, string deviceToken, HttpClient? client = null)
    {
        _root = Activation.CloudApiRoot(baseUrl);
        _ownsClient = client is null;
        _client = client ?? new HttpClient { Timeout = Timeout };
        _client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", deviceToken);
    }

    public async Task<IReadOnlyList<ItemAck>> PushAsync(PushBatch batch, CancellationToken cancellation)
    {
        var payload = new JsonObject
        {
            ["device_id"] = batch.DeviceId,
            ["tenant_id"] = batch.TenantId,
            ["store_id"] = batch.StoreId,
            ["items"] = new JsonArray(batch.Items.Select(item => (JsonNode)new JsonObject
            {
                ["entity_table"] = item.EntityTable,
                ["entity_id"] = item.EntityId,
                ["client_uuid"] = item.ClientUuid,
                ["operation"] = item.Operation,
                // O payload vai como está no outbox: o JsonElement guarda o texto
                // cru de cada número, e nada é reescrito no caminho.
                ["payload"] = JsonNode.Parse(item.Payload.GetRawText()),
            }).ToArray()),
        };
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{_root}/sync/push")
        {
            Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        request.Headers.Add("Idempotency-Key", batch.IdempotencyKey);
        var body = await SendAsync(request, cancellation);

        var acks = new List<ItemAck>();
        if (body.TryGetProperty("results", out var results) && results.ValueKind == JsonValueKind.Array)
        {
            foreach (var entry in results.EnumerateArray())
            {
                if (!entry.TryGetProperty("client_uuid", out var uuid) || uuid.ValueKind != JsonValueKind.String)
                {
                    throw new TransportException("Resposta ilegível do servidor: resultado sem client_uuid");
                }
                acks.Add(new ItemAck(
                    uuid.GetString()!,
                    ItemStatuses.Parse(entry.TryGetProperty("status", out var status) && status.ValueKind == JsonValueKind.String ? status.GetString() : null),
                    entry.TryGetProperty("message", out var message) && message.ValueKind == JsonValueKind.String ? message.GetString() : null,
                    entry.TryGetProperty("server_seq", out var seq) && seq.ValueKind == JsonValueKind.Number ? seq.GetInt64() : null));
            }
        }
        return acks;
    }

    public async Task<PullResponse> PullAsync(PullRequest pull, CancellationToken cancellation)
    {
        var query = string.Join("&", new[]
        {
            ("tenant_id", pull.TenantId), ("store_id", pull.StoreId), ("entity_table", pull.EntityTable),
            ("since", pull.SinceServerSeq.ToString(CultureInfo.InvariantCulture)),
            ("limit", pull.Limit.ToString(CultureInfo.InvariantCulture)),
        }.Select(pair => $"{pair.Item1}={Uri.EscapeDataString(pair.Item2)}"));
        using var request = new HttpRequestMessage(HttpMethod.Get, $"{_root}/sync/pull?{query}");
        var body = await SendAsync(request, cancellation);

        var rows = body.TryGetProperty("rows", out var list) && list.ValueKind == JsonValueKind.Array
            ? list.EnumerateArray().Select(row => row.Clone()).ToList()
            : [];
        var last = body.TryGetProperty("last_server_seq", out var seq) ? ToLong(seq) ?? pull.SinceServerSeq : pull.SinceServerSeq;
        var more = body.TryGetProperty("has_more", out var hasMore) && hasMore.ValueKind == JsonValueKind.True;
        return new PullResponse(pull.EntityTable, rows, last, more);
    }

    public async Task<long> HeartbeatAsync(TerminalHealth health, CancellationToken cancellation)
    {
        var payload = new JsonObject
        {
            ["device_id"] = health.DeviceId,
            ["tenant_id"] = health.TenantId,
            ["terminal_clock"] = health.TerminalClock,
            ["pending_items"] = health.PendingItems,
            ["quarantined_items"] = health.QuarantinedItems,
            ["oldest_pending_at"] = health.OldestPendingAt,
            ["last_quarantine_reason"] = health.LastQuarantineReason,
        };
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{_root}/devices/heartbeat")
        {
            Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        var body = await SendAsync(request, cancellation);
        return body.TryGetProperty("clock_drift_ms", out var drift) ? ToLong(drift) ?? 0 : 0;
    }

    // -- comandos do painel --------------------------------------------------

    /// <summary>
    /// <c>GET /commands/pending</c>. Um comando malformado — ou de um tipo que
    /// este caixa não conhece — é descartado, não fatal: não pode impedir os
    /// outros de chegar, e ordem que não se entende não se obedece.
    /// </summary>
    public async Task<IReadOnlyList<Pdv.Core.Remote.RemoteCommand>> FetchCommandsAsync(
        string tenantId, string storeId, string deviceId, int limit, CancellationToken cancellation)
    {
        var query = $"tenant_id={Uri.EscapeDataString(tenantId)}&store_id={Uri.EscapeDataString(storeId)}" +
                    $"&device_id={Uri.EscapeDataString(deviceId)}&limit={limit}";
        using var request = new HttpRequestMessage(HttpMethod.Get, $"{_root}/commands/pending?{query}");
        var body = await SendAsync(request, cancellation);
        var commands = new List<Pdv.Core.Remote.RemoteCommand>();
        if (!body.TryGetProperty("commands", out var list) || list.ValueKind != JsonValueKind.Array) return commands;
        foreach (var entry in list.EnumerateArray())
        {
            if (ParseCommand(entry) is { } command) commands.Add(command);
        }
        return commands;
    }

    /// <summary>
    /// Só a forma: quem confere assinatura, validade e teto é o
    /// <c>RemoteCommandService</c>. Decidir segurança na camada de rede espalharia
    /// a decisão por duas camadas.
    /// </summary>
    internal static Pdv.Core.Remote.RemoteCommand? ParseCommand(JsonElement entry)
    {
        if (entry.ValueKind != JsonValueKind.Object) return null;
        string? Field(string name) =>
            entry.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

        var kind = Field("kind");
        if (kind is null || !Pdv.Core.Remote.CommandProtocol.Kinds.Contains(kind)) return null;

        JsonElement payload;
        if (!entry.TryGetProperty("payload", out var raw) || raw.ValueKind == JsonValueKind.Null)
        {
            payload = JsonDocument.Parse("{}").RootElement.Clone();
        }
        else if (raw.ValueKind == JsonValueKind.Object)
        {
            payload = raw.Clone();
        }
        else
        {
            return null;
        }

        var uuid = Field("command_uuid");
        var tenant = Field("tenant_id");
        var store = Field("store_id");
        var device = Field("device_id");
        var issuedBy = Field("issued_by_user_id");
        var issuedAt = Field("issued_at");
        var signature = Field("signature");
        if (uuid is null || tenant is null || store is null || device is null || issuedBy is null ||
            issuedAt is null || signature is null)
        {
            return null;
        }
        return new Pdv.Core.Remote.RemoteCommand(
            uuid, tenant, store, device, kind, payload, issuedBy, Field("issued_by_name") ?? "", issuedAt, signature);
    }

    /// <summary><c>POST /commands/results</c>. A espera viaja em campo à parte: uma nuvem antiga a ignora sem recusar os resultados.</summary>
    public async Task<IReadOnlyList<string>> ReportCommandsAsync(
        string tenantId, string storeId, string deviceId,
        IReadOnlyList<Remote.CommandResult> results, IReadOnlyList<Remote.AwaitingNotice> awaiting,
        CancellationToken cancellation)
    {
        var payload = new JsonObject
        {
            ["tenant_id"] = tenantId,
            ["store_id"] = storeId,
            ["device_id"] = deviceId,
            ["results"] = new JsonArray(results.Select(result => (JsonNode)new JsonObject
            {
                ["command_uuid"] = result.CommandUuid,
                ["status"] = result.Status,
                ["message"] = result.Message,
                ["settled_at"] = result.SettledAt,
            }).ToArray()),
            ["awaiting"] = new JsonArray(awaiting.Select(notice => (JsonNode)new JsonObject
            {
                ["command_uuid"] = notice.CommandUuid,
                ["message"] = notice.Message,
                ["requested_at"] = notice.RequestedAt,
            }).ToArray()),
        };
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{_root}/commands/results")
        {
            Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        var body = await SendAsync(request, cancellation);
        // Só o que a nuvem nomear sai da fila de relato; vazio é "não confirmei nada".
        return body.TryGetProperty("accepted", out var accepted) && accepted.ValueKind == JsonValueKind.Array
            ? accepted.EnumerateArray().Where(value => value.ValueKind == JsonValueKind.String)
                .Select(value => value.GetString()!).ToList()
            : [];
    }

    /// <summary>Os códigos de erro tratados igual em toda rota.</summary>
    internal async Task<JsonElement> SendAsync(HttpRequestMessage request, CancellationToken cancellation)
    {
        int status;
        string text;
        try
        {
            using var response = await _client.SendAsync(request, cancellation);
            status = (int)response.StatusCode;
            text = await response.Content.ReadAsStringAsync(cancellation);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException or IOException)
        {
            if (cancellation.IsCancellationRequested) throw;
            throw new TransportException($"Falha de rede: {error.Message}", error);
        }

        if (status is 401 or 403) throw new AuthException($"Terminal não autorizado (HTTP {status})");
        if (status >= 400) throw new TransportException($"HTTP {status}: {text[..Math.Min(200, text.Length)]}");
        try
        {
            using var document = JsonDocument.Parse(text);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new TransportException("Resposta ilegível do servidor: não é um objeto JSON");
            }
            return document.RootElement.Clone();
        }
        catch (JsonException error)
        {
            throw new TransportException($"Resposta ilegível do servidor: {error.Message}", error);
        }
    }

    private static long? ToLong(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.Number when value.TryGetInt64(out var number) => number,
        JsonValueKind.String when long.TryParse(value.GetString(), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var number) => number,
        _ => null,
    };

    public void Dispose()
    {
        if (_ownsClient) _client.Dispose();
    }
}
