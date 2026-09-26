using System.Globalization;
using System.Text.Json.Nodes;
using Pdv.Core;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

public sealed class TicketNotFoundException(string message) : Exception(message);

public sealed class InvalidTransitionException(string message) : Exception(message);

/// <summary>Um ticket da cozinha, como a tela do KDS o mostra.</summary>
public sealed record KdsTicket(
    string Id, string OrderId, long LocalNumber, string TableLabel, string Station, string ProductName,
    string Quantity, string Notes, string Status, string QueuedAt, long WaitingSeconds)
{
    public bool IsLate => Status is "queued" or "preparing" && WaitingSeconds >= KdsService.LateThresholdMinutes * 60;

    public JsonObject ToJson() => new()
    {
        ["ticket_id"] = Id,
        ["order_id"] = OrderId,
        ["local_number"] = LocalNumber,
        ["table_label"] = TableLabel,
        ["station"] = Station,
        ["product_name"] = ProductName,
        ["quantity"] = Quantity,
        ["notes"] = Notes,
        ["status"] = Status,
        ["queued_at"] = QueuedAt,
        ["waiting_seconds"] = WaitingSeconds,
        ["is_late"] = IsLate,
    };
}

/// <summary>
/// A fila da cozinha — o <c>edge/kds.py</c>. O ciclo é o da cozinha, não o da venda:
/// <c>queued → preparing → ready → delivered</c>, com recall para voltar.
/// </summary>
/// <remarks>
/// Recall existe porque a cozinha erra, e desfazer o "pronto" não pode exigir
/// cancelar o item, que mexe na venda e é decisão de gerente. Num recall, o
/// carimbo das etapas que ficaram à frente sai: manter o <c>ready_at</c> de um
/// prato que voltou à chapa faria a cozinha parecer mais rápida justamente
/// quando errou.
/// </remarks>
public sealed class KdsService(PdvDatabase database, TerminalIdentity terminal, EventHub? hub = null, TimeProvider? clock = null)
{
    public const int LateThresholdMinutes = 15;

    private static readonly Dictionary<string, string[]> Transitions = new(StringComparer.Ordinal)
    {
        ["queued"] = ["preparing", "canceled"],
        ["preparing"] = ["ready", "queued", "canceled"],
        ["ready"] = ["delivered", "preparing"],
        ["delivered"] = ["ready"],
        ["canceled"] = [],
    };

    private static readonly (string Name, string? Column)[] Stages =
        [("queued", null), ("preparing", "started_at"), ("ready", "ready_at"), ("delivered", "delivered_at")];

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly EventHub _hub = hub ?? new EventHub(clock);

    private const string Select =
        "SELECT t.id, t.order_id, o.local_number, o.customer_id, t.station, t.product_name, t.quantity, t.notes, " +
        "t.status, t.queued_at FROM kds_tickets t JOIN orders o ON o.id = t.order_id ";

    /// <summary>O que ainda importa à cozinha: entregue e cancelado ficam de fora.</summary>
    public IReadOnlyList<KdsTicket> ListActive(string? station = null)
    {
        var sql = Select + "WHERE t.tenant_id = $tenant AND t.status IN ('queued','preparing','ready') ";
        if (!string.IsNullOrEmpty(station)) sql += "AND t.station = $station ";
        sql += "ORDER BY t.queued_at";
        return Query(sql, ("$tenant", terminal.TenantId), ("$station", station));
    }

    public KdsTicket Get(string ticketId) => Require(ticketId);

    public KdsTicket Advance(string ticketId, string toStatus)
    {
        var current = Require(ticketId);
        if (!Transitions.TryGetValue(current.Status, out var allowed) || !allowed.Contains(toStatus))
        {
            throw new InvalidTransitionException(
                $"Não é possível ir de {TableService.PyRepr(current.Status)} para {TableService.PyRepr(toStatus)}.");
        }

        var now = Iso.Now(_clock);
        var assignments = new List<string> { "status = $status", "updated_at = $now" };
        var target = Array.FindIndex(Stages, stage => stage.Name == toStatus);
        if (target >= 0)
        {
            if (Stages[target].Column is { } own) assignments.Add($"{own} = $now");
            foreach (var (_, column) in Stages[(target + 1)..])
            {
                if (column is not null) assignments.Add($"{column} = NULL");
            }
        }
        database.InTransaction(transaction => transaction.Command(
            $"UPDATE kds_tickets SET {string.Join(", ", assignments)} WHERE id = $id",
            ("$status", toStatus), ("$now", now), ("$id", ticketId)).ExecuteNonQuery());

        var updated = Require(ticketId);
        _hub.Publish("ticket.changed", updated.ToJson());
        return updated;
    }

    /// <summary>Avanço natural: fila → preparo → pronto → entregue.</summary>
    public KdsTicket Bump(string ticketId)
    {
        var current = Require(ticketId);
        var next = current.Status switch
        {
            "queued" => "preparing",
            "preparing" => "ready",
            "ready" => "delivered",
            _ => throw new InvalidTransitionException(
                $"Ticket já está {TableService.PyRepr(current.Status)}; não há próximo passo."),
        };
        return Advance(ticketId, next);
    }

    /// <summary>Desfaz o último avanço — a cozinha bateu pronto no prato errado.</summary>
    public KdsTicket Recall(string ticketId)
    {
        var current = Require(ticketId);
        var previous = current.Status switch
        {
            "preparing" => "queued",
            "ready" => "preparing",
            "delivered" => "ready",
            _ => throw new InvalidTransitionException(
                $"Ticket está {TableService.PyRepr(current.Status)}; não há o que desfazer."),
        };
        return Advance(ticketId, previous);
    }

    private KdsTicket Require(string ticketId) =>
        Query(Select + "WHERE t.id = $id AND t.tenant_id = $tenant", ("$id", ticketId), ("$tenant", terminal.TenantId))
            .FirstOrDefault() ?? throw new TicketNotFoundException($"Ticket {ticketId} não encontrado.");

    private List<KdsTicket> Query(string sql, params (string Name, object? Value)[] parameters)
    {
        var now = _clock.GetUtcNow();
        using var command = Sql.Command(database.Connection, null, sql, parameters);
        using var reader = command.ExecuteReader();
        var tickets = new List<KdsTicket>();
        while (reader.Read())
        {
            var queuedAt = reader.GetString(9);
            var waiting = DateTimeOffset.TryParse(queuedAt, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var queued)
                ? Math.Max(0, (long)(now - queued).TotalSeconds)
                : 0;
            tickets.Add(new KdsTicket(
                reader.GetString(0), reader.GetString(1), reader.GetInt64(2),
                reader.IsDBNull(3) ? "" : reader.GetString(3), reader.GetString(4), reader.GetString(5),
                reader.GetString(6), reader.IsDBNull(7) ? "" : reader.GetString(7), reader.GetString(8),
                queuedAt, waiting));
        }
        return tickets;
    }
}
