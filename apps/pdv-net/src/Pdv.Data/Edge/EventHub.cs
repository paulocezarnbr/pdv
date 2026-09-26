using System.Text.Json.Nodes;
using System.Threading.Channels;
using Pdv.Core;

namespace Pdv.Data.Edge;

/// <summary>Um fato já gravado — nunca um pedido de ação.</summary>
public sealed record EdgeEvent(string Kind, JsonObject Payload, string At)
{
    /// <summary>O <c>to_json</c> do Python: <c>kind</c>, <c>at</c> e o payload achatado.</summary>
    public JsonObject ToJson()
    {
        var json = new JsonObject { ["kind"] = Kind, ["at"] = At };
        foreach (var (key, value) in Payload) json[key] = value?.DeepClone();
        return json;
    }
}

/// <summary>A ponta de leitura de um assinante (uma tela do KDS, um app de garçom).</summary>
public sealed class EdgeSubscription : IDisposable
{
    private readonly EventHub _hub;
    private readonly Channel<EdgeEvent> _queue;

    internal EdgeSubscription(EventHub hub, IReadOnlySet<string> topics)
    {
        _hub = hub;
        Topics = topics;
        // Fila cheia descarta o MAIS ANTIGO: a tela atrasada precisa do estado
        // atual, e se reconcilia buscando a lista completa ao reconectar.
        _queue = Channel.CreateBounded<EdgeEvent>(new BoundedChannelOptions(EventHub.MaxQueueSize)
        {
            FullMode = BoundedChannelFullMode.DropOldest,
            SingleReader = true,
        }, _ => Interlocked.Increment(ref _dropped));
    }

    private int _dropped;

    public IReadOnlySet<string> Topics { get; }

    /// <summary>Quantos eventos esta tela perdeu por ficar para trás.</summary>
    public int Dropped => _dropped;

    internal void Offer(EdgeEvent edgeEvent) => _queue.Writer.TryWrite(edgeEvent);

    public async Task<EdgeEvent?> NextAsync(TimeSpan timeout, CancellationToken cancellation = default)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(timeout);
        try
        {
            return await _queue.Reader.ReadAsync(deadline.Token);
        }
        catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
        {
            return null;
        }
    }

    public bool TryNext(out EdgeEvent? edgeEvent) => _queue.Reader.TryRead(out edgeEvent);

    public void Dispose() => _hub.Unsubscribe(this);
}

/// <summary>
/// O barramento do salão — o <c>edge/hub.py</c>: quem grava avisa quem está olhando.
/// </summary>
/// <remarks>
/// Uma fila por assinante, e publicar nunca espera: um tablet congelado na
/// cozinha não pode segurar a venda que está sendo gravada no caixa.
/// </remarks>
public sealed class EventHub(TimeProvider? clock = null)
{
    public const int MaxQueueSize = 200;

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Lock _lock = new();
    private readonly List<EdgeSubscription> _subscribers = [];

    /// <summary>Assina os tópicos dados; nenhum recebe tudo.</summary>
    public EdgeSubscription Subscribe(IEnumerable<string>? topics = null)
    {
        var subscription = new EdgeSubscription(this, new HashSet<string>(topics ?? [], StringComparer.Ordinal));
        lock (_lock) _subscribers.Add(subscription);
        return subscription;
    }

    internal void Unsubscribe(EdgeSubscription subscription)
    {
        lock (_lock) _subscribers.Remove(subscription);
    }

    /// <summary>Entrega a quem interessa e devolve quantos receberam.</summary>
    public int Publish(string kind, JsonObject payload)
    {
        var edgeEvent = new EdgeEvent(kind, payload, Iso.Now(_clock));
        EdgeSubscription[] targets;
        lock (_lock)
        {
            targets = [.. _subscribers.Where(s => s.Topics.Count == 0 || s.Topics.Contains(kind))];
        }
        foreach (var target in targets) target.Offer(edgeEvent);
        return targets.Length;
    }
}
