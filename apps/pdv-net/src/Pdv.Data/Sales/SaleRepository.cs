using Microsoft.Data.Sqlite;
using Pdv.Core;

namespace Pdv.Data.Sales;

/// <summary>Quem é este terminal: vai em toda linha e em todo payload.</summary>
public sealed record TerminalIdentity(string TenantId, string StoreId, string DeviceId);

public sealed record OpenOrder(
    string Id, string ClientUuid, long LocalNumber, long SubtotalCents, long DiscountCents, long TotalCents);

public sealed class OrderNotOpenException(string message) : Exception(message);

/// <summary>Pedido e pagamento — mesmas linhas e mesmos payloads do <c>SaleRepository</c> do Python.</summary>
/// <remarks>
/// Os payloads são conferidos contra <c>contracts/push-day.json</c> nos testes:
/// uma chave faltando e a nuvem recusa o lote inteiro (foi o que aconteceu na
/// 1.1.2 do PDV em Python, com o número da venda).
/// </remarks>
public sealed class SaleRepository(TerminalIdentity terminal, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    /// <summary>Sequência local, na transação de quem consome o número.</summary>
    public static long NextCounter(SqliteTransaction transaction, string name)
    {
        using (var upsert = transaction.Command(
                   "INSERT INTO local_counters (name, value) VALUES ($name, 1) " +
                   "ON CONFLICT (name) DO UPDATE SET value = value + 1",
                   ("$name", name)))
        {
            upsert.ExecuteNonQuery();
        }
        using var read = transaction.Command("SELECT value FROM local_counters WHERE name = $name", ("$name", name));
        return Convert.ToInt64(read.ExecuteScalar());
    }

    /// <summary>Abre o pedido já gravado: uma queda entre dois itens não perde o primeiro.</summary>
    public OpenOrder CreateOrder(SqliteTransaction transaction, string operatorId, string channel = "counter")
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var localNumber = NextCounter(transaction, "order_local_number");
        var now = Iso.Now(_clock);
        using var insert = transaction.Command(
            """
            INSERT INTO orders
                (id, tenant_id, store_id, device_id, local_number, channel, status,
                 operator_id, opened_at, created_at, updated_at, origin_device_id, client_uuid, is_synced)
            VALUES ($id, $tenant, $store, $device, $number, $channel, 'open',
                    $operator, $now, $now, $now, $device, $uuid, 0)
            """,
            ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
            ("$device", terminal.DeviceId), ("$number", localNumber), ("$channel", channel),
            ("$operator", operatorId), ("$now", now), ("$uuid", clientUuid));
        insert.ExecuteNonQuery();
        return new OpenOrder(id, clientUuid, localNumber, 0, 0, 0);
    }

    public static OpenOrder LoadOpenOrder(SqliteConnection connection, string orderId) =>
        LoadOpenOrder(connection, null, orderId);

    /// <summary>Dentro de uma transação aberta, o comando precisa dela (Microsoft.Data.Sqlite exige).</summary>
    public static OpenOrder LoadOpenOrder(SqliteTransaction transaction, string orderId) =>
        LoadOpenOrder(transaction.Connection!, transaction, orderId);

    private static OpenOrder LoadOpenOrder(SqliteConnection connection, SqliteTransaction? transaction, string orderId)
    {
        using var command = Sql.Command(
            connection, transaction,
            "SELECT id, client_uuid, local_number, subtotal_cents, discount_cents, total_cents, status " +
            "FROM orders WHERE id = $id",
            ("$id", orderId));
        using var reader = command.ExecuteReader();
        if (!reader.Read())
        {
            throw new OrderNotOpenException($"Pedido {orderId} não existe.");
        }
        if (reader.GetString(6) != "open")
        {
            throw new OrderNotOpenException($"Pedido {orderId} está {reader.GetString(6)}, não aberto.");
        }
        return new OpenOrder(
            reader.GetString(0), reader.GetString(1), reader.GetInt64(2),
            reader.GetInt64(3), reader.GetInt64(4), reader.GetInt64(5));
    }

    /// <summary>Totais a partir dos itens vivos no banco — a fonte da verdade, não a memória da tela.</summary>
    /// <remarks>
    /// <para>
    /// O desconto é guardado em centavos e segue valendo quando entra ou sai um
    /// item, como no Python.
    /// </para>
    /// <para>
    /// Regra a mais que o Python: o desconto nunca passa do subtotal. Lá, um
    /// cancelamento depois do desconto deixava <c>discount_cents</c> maior que
    /// o subtotal (com o total travado em zero). A nota fiscal rateia o desconto
    /// pelos itens e não fecha com isso.
    /// </para>
    /// </remarks>
    public static OpenOrder RecomputeTotals(SqliteTransaction transaction, string orderId, TimeProvider? clock = null)
    {
        long subtotal, discount;
        using (var sums = transaction.Command(
                   "SELECT COALESCE(SUM(total_cents), 0), (SELECT discount_cents FROM orders WHERE id = $order) " +
                   "FROM order_items WHERE order_id = $order AND canceled_at IS NULL",
                   ("$order", orderId)))
        using (var reader = sums.ExecuteReader())
        {
            reader.Read();
            subtotal = reader.GetInt64(0);
            discount = reader.IsDBNull(1) ? 0 : reader.GetInt64(1);
        }
        return StoreTotals(transaction, orderId, subtotal, Math.Min(discount, subtotal), clock);
    }

    /// <summary>Grava subtotal, desconto e total (<c>update_totals</c>) e devolve o pedido como ficou.</summary>
    public static OpenOrder StoreTotals(
        SqliteTransaction transaction, string orderId, long subtotal, long discount, TimeProvider? clock = null)
    {
        var total = Math.Max(0, subtotal - discount);
        using (var update = transaction.Command(
                   "UPDATE orders SET subtotal_cents = $sub, discount_cents = $disc, total_cents = $total, " +
                   "updated_at = $now WHERE id = $id",
                   ("$sub", subtotal), ("$disc", discount), ("$total", total), ("$now", Iso.Now(clock)),
                   ("$id", orderId)))
        {
            update.ExecuteNonQuery();
        }
        return LoadOpenOrder(transaction, orderId);
    }

    /// <summary>Fecha o pedido e o anuncia à nuvem com a ficha inteira.</summary>
    public void CloseOrder(SqliteTransaction transaction, OpenOrder order)
    {
        var now = Iso.Now(_clock);
        using (var update = transaction.Command(
                   "UPDATE orders SET status = 'paid', closed_at = $now, updated_at = $now, " +
                   "subtotal_cents = $subtotal, discount_cents = $discount, total_cents = $total " +
                   "WHERE id = $id AND status = 'open'",
                   ("$now", now), ("$subtotal", order.SubtotalCents), ("$discount", order.DiscountCents),
                   ("$total", order.TotalCents), ("$id", order.Id)))
        {
            if (update.ExecuteNonQuery() != 1)
            {
                throw new OrderNotOpenException($"Pedido {order.Id} já não está aberto.");
            }
        }

        string channel, operatorId, openedAt;
        using (var opened = transaction.Command(
                   "SELECT channel, operator_id, opened_at FROM orders WHERE id = $id", ("$id", order.Id)))
        using (var reader = opened.ExecuteReader())
        {
            reader.Read();
            channel = reader.GetString(0);
            operatorId = reader.GetString(1);
            openedAt = reader.GetString(2);
        }

        _outbox.Enqueue(transaction, "orders", order.Id, order.ClientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = order.Id,
            ["tenant_id"] = terminal.TenantId,
            ["store_id"] = terminal.StoreId,
            ["device_id"] = terminal.DeviceId,
            ["status"] = "paid",
            ["local_number"] = order.LocalNumber,
            ["channel"] = channel,
            ["operator_id"] = operatorId,
            ["opened_at"] = openedAt,
            ["subtotal_cents"] = order.SubtotalCents,
            ["discount_cents"] = order.DiscountCents,
            ["total_cents"] = order.TotalCents,
            ["closed_at"] = now,
            ["client_uuid"] = order.ClientUuid,
        });
    }

    /// <summary>Grava as formas de pagamento, na transação que fecha o pedido.</summary>
    /// <remarks>
    /// No cartão, o <c>client_uuid</c> da linha é o <c>TransactionId</c> do TEF.
    /// É por ele que a recuperação sabe, depois de uma queda, se a venda de uma
    /// aprovação pendente chegou ao banco (<see cref="WasRecorded"/>).
    /// </remarks>
    public void RecordPayments(SqliteTransaction transaction, string orderId, IEnumerable<Payment> payments)
    {
        var now = Iso.Now(_clock);
        foreach (var payment in payments)
        {
            var id = Iso.NewId();
            using (var insert = transaction.Command(
                       """
                       INSERT INTO payments
                           (id, order_id, tenant_id, method, amount_cents, change_cents, nsu, created_at, client_uuid)
                       VALUES ($id, $order, $tenant, $method, $amount, $change, $nsu, $now, $uuid)
                       """,
                       ("$id", id), ("$order", orderId), ("$tenant", terminal.TenantId),
                       ("$method", payment.Method), ("$amount", payment.AmountCents),
                       ("$change", payment.ChangeCents), ("$nsu", payment.Nsu), ("$now", now),
                       ("$uuid", payment.ClientUuid)))
            {
                insert.ExecuteNonQuery();
            }

            var payload = new Dictionary<string, object?>
            {
                ["id"] = id,
                ["order_id"] = orderId,
                ["method"] = payment.Method,
                ["amount_cents"] = payment.AmountCents,
                ["change_cents"] = payment.ChangeCents,
                ["created_at"] = now,
            };
            if (payment.Nsu is not null)
            {
                // A nuvem já aceita (payments.nsu desde 001_init): é o que
                // concilia a venda com o extrato da adquirente.
                payload["nsu"] = payment.Nsu;
            }
            _outbox.Enqueue(transaction, "payments", id, payment.ClientUuid, "insert", payload);
        }
    }

    /// <summary>A venda da transação TEF chegou ao banco?</summary>
    public static bool WasRecorded(SqliteConnection connection, string tefTransactionId)
    {
        using var command = Sql.Command(
            connection, null, "SELECT 1 FROM payments WHERE client_uuid = $uuid", ("$uuid", tefTransactionId));
        return command.ExecuteScalar() is not null;
    }
}
