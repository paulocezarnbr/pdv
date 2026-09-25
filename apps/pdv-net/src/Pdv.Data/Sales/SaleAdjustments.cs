using System.Globalization;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Scale;
using Pdv.Core.Stock;
using Pdv.Data.Auth;

namespace Pdv.Data.Sales;

/// <summary>Operação que pede autorização e não a recebeu (ou recebeu de quem não pode).</summary>
public sealed class AuthorizationRequiredException(string message) : Exception(message);

/// <summary>Cancelamento de item e desconto — o <c>cancel_item</c> e o <c>apply_discount</c> do Python.</summary>
/// <remarks>
/// <para>
/// Quem autoriza chega aqui já conferido por PIN (<see cref="StaffAuthentication"/>).
/// Mesmo assim o papel e o teto são conferidos de novo <b>dentro</b> da
/// transação, contra o cadastro como ele está agora: um gerente desativado
/// entre o PIN e o clique não libera nada.
/// </para>
/// <para>
/// Nada é apagado. O item cancelado recebe <c>canceled_at</c>, o estoque volta
/// com um movimento positivo e o evento vai ao ledger como <c>critical</c>,
/// com quem pediu e quem liberou. Cancelamento é o vetor de furto nº 1 no PDV.
/// </para>
/// </remarks>
public sealed class SaleAdjustments(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    /// <summary>Cancela um item vivo da venda aberta. Só gerente libera.</summary>
    /// <exception cref="AuthorizationRequiredException">Quem autorizou não é gerente ativo com poder de autorizar.</exception>
    /// <exception cref="InvalidQuantityException">Item inexistente, de outra venda ou já cancelado; motivo vazio.</exception>
    public OpenOrder CancelItem(string orderId, string itemId, string operatorId, Identity authorizer, string reason)
    {
        reason = reason.Trim();
        if (reason.Length == 0) throw new InvalidQuantityException("Informe o motivo do cancelamento.");

        return database.InTransaction(transaction =>
        {
            SaleRepository.LoadOpenOrder(transaction, orderId);
            RequireAuthorizer(transaction, authorizer.Id, Roles.ItemCancel,
                "Cancelamento de item exige autorização de gerente.");

            var item = LoadLiveItem(transaction, orderId, itemId)
                       ?? throw new InvalidQuantityException("Item inexistente na venda atual");

            var canceledAt = Iso.Now(_clock);
            using (var update = transaction.Command(
                       "UPDATE order_items SET canceled_at = $at, canceled_by_user_id = $by, cancel_reason = $reason " +
                       "WHERE id = $id AND canceled_at IS NULL",
                       ("$at", canceledAt), ("$by", authorizer.Id), ("$reason", reason), ("$id", itemId)))
            {
                update.ExecuteNonQuery();
            }
            // client_uuid novo: é uma mudança, não o item de novo. Com o do item,
            // a nuvem leria como reenvio e descartaria — e o item cancelado
            // seguiria vivo no "mais vendidos" do painel.
            _outbox.Enqueue(transaction, "order_items", itemId, Iso.NewId(), "update", new Dictionary<string, object?>
            {
                ["id"] = itemId,
                ["canceled_at"] = canceledAt,
                ["canceled_by_user_id"] = authorizer.Id,
                ["cancel_reason"] = reason,
            });

            foreach (var (inventoryItemId, consumedMg) in item.Consumptions)
            {
                Reverse(transaction, inventoryItemId, consumedMg, itemId);
            }

            ledger.Append(transaction, "item_canceled", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = orderId,
                ["order_item_id"] = itemId,
                ["product_name"] = item.ProductName,
                ["net_grams"] = item.NetWeightGrams,
                ["total_cents"] = item.TotalCents,
                ["reason"] = reason,
            }, severity: "critical", authorizerUserId: authorizer.Id);

            return SaleRepository.RecomputeTotals(transaction, orderId, _clock);
        });
    }

    /// <summary>Desconto percentual sobre o subtotal. Substitui o anterior; não acumula.</summary>
    /// <remarks>
    /// O que fica gravado é o valor em <b>centavos</b>: o percentual some no
    /// arredondamento, e a conciliação do caixa fecha sobre o valor.
    /// </remarks>
    /// <exception cref="AuthorizationRequiredException">Sem poder de autorizar, ou acima do teto do perfil.</exception>
    public (long DiscountCents, OpenOrder Order) ApplyDiscount(
        string orderId, decimal percent, string operatorId, Identity authorizer, string reason)
    {
        reason = reason.Trim();
        if (reason.Length == 0) throw new InvalidQuantityException("Informe o motivo do desconto.");
        if (percent < 0 || percent > 100) throw new InvalidQuantityException("Desconto precisa estar entre 0% e 100%");

        return database.InTransaction(transaction =>
        {
            var order = SaleRepository.RecomputeTotals(transaction, orderId, _clock);
            if (order.SubtotalCents <= 0) throw new InvalidQuantityException("Não há venda aberta para aplicar desconto");

            // Regra a mais que o Python: lá o teto era conferido só no diálogo.
            var ceiling = RequireAuthorizer(transaction, authorizer.Id, null,
                $"{authorizer.Name} não tem permissão para autorizar esta operação.");
            if (percent > ceiling)
            {
                throw new AuthorizationRequiredException(
                    $"{authorizer.Name} pode conceder até {ceiling.ToString(CultureInfo.InvariantCulture)}% — " +
                    $"o pedido é de {percent.ToString(CultureInfo.InvariantCulture)}%.");
            }

            var discount = WeightPricing.DiscountFor(order.SubtotalCents, percent);
            order = SaleRepository.StoreTotals(transaction, orderId, order.SubtotalCents, discount, _clock);

            ledger.Append(transaction, "discount_applied", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = orderId,
                ["percent"] = percent.ToString(CultureInfo.InvariantCulture),
                ["subtotal_cents"] = order.SubtotalCents,
                ["discount_cents"] = discount,
                ["total_cents"] = order.TotalCents,
                ["reason"] = reason,
            }, severity: "warning", authorizerUserId: authorizer.Id);

            return (discount, order);
        });
    }

    /// <summary>Os itens vivos da venda, para a tela escolher qual cancelar.</summary>
    public IReadOnlyList<LiveItem> LiveItems(string orderId)
    {
        using var command = Sql.Command(
            database.Connection, null,
            "SELECT id, product_name, pricing_mode, quantity, net_weight_grams, unit_price_cents, total_cents " +
            "FROM order_items WHERE order_id = $order AND canceled_at IS NULL ORDER BY created_at, rowid",
            ("$order", orderId));
        using var reader = command.ExecuteReader();
        var items = new List<LiveItem>();
        while (reader.Read())
        {
            items.Add(new LiveItem(
                reader.GetString(0), reader.GetString(1), reader.GetString(2),
                decimal.Parse(reader.GetValue(3).ToString()!, NumberStyles.Float, CultureInfo.InvariantCulture),
                reader.GetInt64(4), reader.GetInt64(5), reader.GetInt64(6)));
        }
        return items;
    }

    /// <summary>
    /// Confere quem autorizou contra o cadastro de agora: ativo, com poder de
    /// autorizar e, quando pedido, no papel certo. Devolve o teto de desconto.
    /// </summary>
    private decimal RequireAuthorizer(
        SqliteTransaction transaction, string userId, IReadOnlySet<string>? roles, string message)
    {
        using var command = transaction.Command(
            "SELECT role, can_authorize, max_discount_percent FROM users " +
            "WHERE id = $id AND tenant_id = $tenant AND is_active = 1",
            ("$id", userId), ("$tenant", terminal.TenantId));
        using var reader = command.ExecuteReader();
        if (!reader.Read() || reader.GetInt64(1) == 0 || (roles is not null && !roles.Contains(reader.GetString(0))))
        {
            throw new AuthorizationRequiredException(message);
        }
        return reader.IsDBNull(2)
            ? 0m
            : decimal.TryParse(reader.GetValue(2).ToString(), NumberStyles.Number, CultureInfo.InvariantCulture, out var ceiling)
                ? ceiling
                : 0m;
    }

    private static CanceledItem? LoadLiveItem(SqliteTransaction transaction, string orderId, string itemId)
    {
        string name;
        long net, total;
        using (var command = transaction.Command(
                   "SELECT product_name, net_weight_grams, total_cents FROM order_items " +
                   "WHERE id = $id AND order_id = $order AND canceled_at IS NULL",
                   ("$id", itemId), ("$order", orderId)))
        using (var reader = command.ExecuteReader())
        {
            if (!reader.Read()) return null;
            name = reader.GetString(0);
            net = reader.GetInt64(1);
            total = reader.GetInt64(2);
        }

        var consumptions = new List<(string, long)>();
        using (var command = transaction.Command(
                   "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients WHERE order_item_id = $id ORDER BY rowid",
                   ("$id", itemId)))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read()) consumptions.Add((reader.GetString(0), reader.GetInt64(1)));
        }
        return new CanceledItem(name, net, total, consumptions);
    }

    /// <summary>O estorno: movimento positivo de ajuste, como o <c>register_movement</c> do cancelamento no Python.</summary>
    private void Reverse(SqliteTransaction transaction, string inventoryItemId, long consumedMg, string orderItemId)
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        using (var insert = transaction.Command(
                   """
                   INSERT INTO stock_movements
                       (id, tenant_id, store_id, inventory_item_id, qty_mg, movement_type, reference_type,
                        reference_id, unit_cost_cents, created_at, origin_device_id, client_uuid, is_synced)
                   VALUES ($id, $tenant, $store, $inventory, $qty, 'adjustment', 'order_item_cancel', $ref, 0, $now,
                           $device, $uuid, 0)
                   """,
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
                   ("$inventory", inventoryItemId), ("$qty", consumedMg), ("$ref", orderItemId), ("$now", now),
                   ("$device", terminal.DeviceId), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        using (var balance = transaction.Command(
                   "UPDATE inventory_items SET balance_mg = balance_mg + $qty, updated_at = $now WHERE id = $id",
                   ("$qty", consumedMg), ("$now", now), ("$id", inventoryItemId)))
        {
            balance.ExecuteNonQuery();
        }

        _outbox.Enqueue(transaction, "stock_movements", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["tenant_id"] = terminal.TenantId,
            ["store_id"] = terminal.StoreId,
            ["inventory_item_id"] = inventoryItemId,
            ["qty_mg"] = consumedMg,
            ["movement_type"] = "adjustment",
            ["reference_type"] = "order_item_cancel",
            ["reference_id"] = orderItemId,
            ["unit_cost_cents"] = 0,
            ["created_at"] = now,
            ["origin_device_id"] = terminal.DeviceId,
            ["client_uuid"] = clientUuid,
        });
    }

    private sealed record CanceledItem(
        string ProductName, long NetWeightGrams, long TotalCents, IReadOnlyList<(string InventoryItemId, long ConsumedMg)> Consumptions);
}

/// <summary>Um item vivo da venda, como a tela mostra.</summary>
public sealed record LiveItem(
    string Id, string ProductName, string PricingMode, decimal Quantity, long NetWeightGrams, long UnitPriceCents, long TotalCents)
{
    public bool IsWeighed => PricingMode == "weight";
}
