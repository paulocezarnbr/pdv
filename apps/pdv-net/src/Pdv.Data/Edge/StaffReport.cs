using System.Text.Json.Nodes;
using Pdv.Core;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

/// <summary>O que uma pessoa fez no período.</summary>
public sealed record WaiterResult(
    string UserId, string Name, string Role, long Orders, long Items, long TotalCents, long TipCents, long OpenOrders)
{
    /// <summary>Ticket médio. Zero comandas dá zero, não divisão por zero.</summary>
    public long AverageTicketCents => Orders > 0 ? TotalCents / Orders : 0;

    public JsonObject ToJson() => new()
    {
        ["user_id"] = UserId,
        ["name"] = Name,
        ["role"] = Role,
        ["orders"] = Orders,
        ["items"] = Items,
        ["total_cents"] = TotalCents,
        ["tip_cents"] = TipCents,
        ["open_orders"] = OpenOrders,
        ["average_ticket_cents"] = AverageTicketCents,
    };
}

/// <summary>
/// Resultado e gorjeta por funcionário — o <c>services/staff_report.py</c>.
/// </summary>
/// <remarks>
/// <para>
/// O dia de food service não começa à meia-noite: a mesa que senta às 23h40 e
/// paga às 00h20 é do turno da noite. O corte é às 5h do fuso da máquina —
/// cortar em UTC partiria o jantar brasileiro ao meio, às 21h.
/// </para>
/// <para>
/// Não é fechamento de caixa: soma o banco deste terminal. A verdade do mês é a
/// nuvem, que recebe os mesmos pedidos pelo outbox.
/// </para>
/// </remarks>
public sealed class StaffReport(PdvDatabase database, string tenantId, TimeProvider? clock = null, TimeZoneInfo? zone = null)
{
    public static readonly TimeSpan BusinessDayStart = TimeSpan.FromHours(5);

    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly TimeZoneInfo _zone = zone ?? TimeZoneInfo.Local;

    /// <summary>Início e fim, em ISO UTC, do dia de operação que contém o instante.</summary>
    public (string Start, string End) BusinessDayWindow(DateTimeOffset? now = null)
    {
        var local = TimeZoneInfo.ConvertTime(now ?? _clock.GetUtcNow(), _zone);
        var start = new DateTimeOffset(local.Date + BusinessDayStart, local.Offset);
        // Ainda é madrugada: pertence ao dia anterior.
        if (local < start) start = start.AddDays(-1);
        return (Iso.Format(start), Iso.Format(start.AddDays(1)));
    }

    /// <summary>
    /// O turno, uma linha por quem atendeu. Só quem atendeu: listar a folha com
    /// zeros viraria ranking de quem estava de folga.
    /// </summary>
    public IReadOnlyList<WaiterResult> ByWaiter((string Start, string End)? window = null)
    {
        var (start, end) = window ?? BusinessDayWindow();
        using var command = Sql.Command(database.Connection, null,
            """
            SELECT o.operator_id                              AS user_id,
                   COALESCE(u.name, 'Sem cadastro')           AS name,
                   COALESCE(u.role, '—')                      AS role,
                   SUM(o.status = 'paid')                     AS orders,
                   SUM(CASE WHEN o.status = 'paid' THEN o.total_cents ELSE 0 END) AS total_cents,
                   SUM(CASE WHEN o.status = 'paid' THEN o.tip_cents ELSE 0 END)   AS tip_cents,
                   SUM(o.status = 'open')                     AS open_orders,
                   (SELECT COUNT(*) FROM order_items i
                     JOIN orders oo ON oo.id = i.order_id
                    WHERE i.canceled_at IS NULL
                      AND oo.tenant_id = o.tenant_id
                      AND oo.channel = 'waiter'
                      AND oo.opened_at >= $start AND oo.opened_at < $end
                      -- Item de quem o lançou quando o app informou; senão, de quem abriu a comanda.
                      AND COALESCE(i.created_by_user_id, oo.operator_id) = o.operator_id) AS items
              FROM orders o
              LEFT JOIN users u ON u.id = o.operator_id
             WHERE o.tenant_id = $tenant
               AND o.channel = 'waiter'
               AND o.status IN ('open', 'paid')
               AND o.opened_at >= $start AND o.opened_at < $end
             GROUP BY o.operator_id
             ORDER BY total_cents DESC, name
            """,
            ("$start", start), ("$end", end), ("$tenant", tenantId));
        using var reader = command.ExecuteReader();
        var results = new List<WaiterResult>();
        while (reader.Read())
        {
            long Number(int i) => reader.IsDBNull(i) ? 0 : reader.GetInt64(i);
            results.Add(new WaiterResult(
                reader.IsDBNull(0) ? "" : reader.GetString(0), reader.GetString(1), reader.GetString(2),
                Number(3), Number(7), Number(4), Number(5), Number(6)));
        }
        return results;
    }

    /// <summary>
    /// O resultado de uma pessoa só, para ela ver no próprio app. Quem ainda não
    /// atendeu ninguém recebe zeros: 404 mostraria falha na primeira abertura do turno.
    /// </summary>
    public JsonObject ForUser(string userId, (string Start, string End)? window = null)
    {
        if (ByWaiter(window).FirstOrDefault(result => result.UserId == userId) is { } found) return found.ToJson();

        var (name, role) = ("Sem cadastro", "—");
        using (var command = Sql.Command(database.Connection, null,
                   "SELECT name, role FROM users WHERE id = $id AND tenant_id = $tenant", ("$id", userId), ("$tenant", tenantId)))
        using (var reader = command.ExecuteReader())
        {
            if (reader.Read()) (name, role) = (reader.GetString(0), reader.GetString(1));
        }
        return new WaiterResult(userId, name, role, 0, 0, 0, 0, 0).ToJson();
    }

    /// <summary>A soma do salão no período — o rodapé do relatório.</summary>
    public JsonObject Totals((string Start, string End)? window = null)
    {
        var results = ByWaiter(window);
        return new JsonObject
        {
            ["orders"] = results.Sum(r => r.Orders),
            ["items"] = results.Sum(r => r.Items),
            ["total_cents"] = results.Sum(r => r.TotalCents),
            ["tip_cents"] = results.Sum(r => r.TipCents),
            ["open_orders"] = results.Sum(r => r.OpenOrders),
        };
    }
}
