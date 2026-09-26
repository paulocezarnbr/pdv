using System.Globalization;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Stock;

namespace Pdv.Data.Sales;

/// <summary>
/// A baixa de insumo por ficha técnica, o estorno e a conferência de saldo —
/// um lugar só para o balcão, o cancelamento e a mesa.
/// </summary>
/// <remarks>
/// Eram duas cópias (registro de item e cancelamento) e a mesa não tinha
/// nenhuma: o prato com ficha saía da cozinha do salão sem mexer no estoque. Uma
/// terceira cópia divergiria das outras na primeira alteração, e o sintoma seria
/// o CMV do painel somar balcão e salão de jeitos diferentes.
/// </remarks>
internal sealed class StockWriter(TerminalIdentity terminal, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);

    /// <summary>O <c>check_availability</c>: avisa, e só trava se a loja pediu.</summary>
    /// <exception cref="InsufficientStockException">Sem saldo, com a loja bloqueando venda negativa.</exception>
    public List<string> CheckAvailability(
        SqliteTransaction transaction, IReadOnlyList<IngredientConsumption> consumptions, bool blockSaleOnNegativeStock)
    {
        var warnings = new List<string>();
        foreach (var consumption in consumptions)
        {
            using var command = transaction.Command(
                "SELECT balance_mg FROM inventory_items WHERE id = $id", ("$id", consumption.InventoryItemId));
            var available = command.ExecuteScalar() is long balance ? balance : 0L;
            if (available >= consumption.ConsumedMg) continue;

            var message = string.Create(CultureInfo.InvariantCulture,
                $"{consumption.InventoryItemName}: saldo {available / 1000m:0.000} g para consumo de {consumption.ConsumedMg / 1000m:0.000} g");
            if (blockSaleOnNegativeStock) throw new InsufficientStockException(message);
            warnings.Add(message);
        }
        return warnings;
    }

    /// <summary>
    /// Grava o consumo do item em <c>order_item_ingredients</c> e devolve as
    /// linhas para o payload. Os ids vão junto: a nuvem grava com o MESMO id e
    /// client_uuid, e um reenvio cai no ON CONFLICT em vez de virar segundo consumo.
    /// </summary>
    public List<Dictionary<string, object?>> InsertIngredients(
        SqliteTransaction transaction, string orderItemId, IReadOnlyList<IngredientConsumption> consumptions, string now)
    {
        var ingredients = new List<Dictionary<string, object?>>();
        foreach (var consumption in consumptions)
        {
            var ingredientId = Iso.NewId();
            var ingredientUuid = Iso.NewId();
            using (var insert = transaction.Command(
                       """
                       INSERT INTO order_item_ingredients
                           (id, order_item_id, inventory_item_id, inventory_item_name, consumed_mg, unit_cost_cents,
                            created_at, client_uuid, is_synced)
                       VALUES ($id, $item, $inventory, $name, $mg, $cost, $now, $uuid, 0)
                       """,
                       ("$id", ingredientId), ("$item", orderItemId), ("$inventory", consumption.InventoryItemId),
                       ("$name", consumption.InventoryItemName), ("$mg", consumption.ConsumedMg),
                       ("$cost", consumption.UnitCostCents), ("$now", now), ("$uuid", ingredientUuid)))
            {
                insert.ExecuteNonQuery();
            }
            ingredients.Add(new Dictionary<string, object?>
            {
                ["id"] = ingredientId,
                ["client_uuid"] = ingredientUuid,
                ["inventory_item_id"] = consumption.InventoryItemId,
                ["inventory_item_name"] = consumption.InventoryItemName,
                ["consumed_mg"] = consumption.ConsumedMg,
                ["unit_cost_cents"] = consumption.UnitCostCents,
            });
        }
        return ingredients;
    }

    /// <summary>Movimento de saída (quantidade negativa) e o cache de saldo, como o <c>register_movement</c>.</summary>
    public void WriteOff(SqliteTransaction transaction, IngredientConsumption consumption, string orderItemId) =>
        Move(transaction, consumption.InventoryItemId, -consumption.ConsumedMg, "sale", "order_item", orderItemId,
            consumption.UnitCostCents);

    /// <summary>O estorno: movimento positivo de ajuste, como o do cancelamento no Python.</summary>
    public void Reverse(SqliteTransaction transaction, string inventoryItemId, long consumedMg, string orderItemId) =>
        Move(transaction, inventoryItemId, consumedMg, "adjustment", "order_item_cancel", orderItemId, 0);

    /// <summary>
    /// Estorna o que o item baixou, pelo consumo GRAVADO e não pela ficha de
    /// hoje: se a receita mudou entre o lançamento e o cancelamento, a ficha nova
    /// devolveria ao estoque o que nunca saiu dele.
    /// </summary>
    public void RestoreItem(SqliteTransaction transaction, string orderItemId)
    {
        var consumed = new List<(string, long)>();
        using (var command = transaction.Command(
                   "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients WHERE order_item_id = $id ORDER BY rowid",
                   ("$id", orderItemId)))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read()) consumed.Add((reader.GetString(0), reader.GetInt64(1)));
        }
        foreach (var (inventoryItemId, mg) in consumed) Reverse(transaction, inventoryItemId, mg, orderItemId);
    }

    private void Move(
        SqliteTransaction transaction, string inventoryItemId, long quantity, string movementType, string referenceType,
        string orderItemId, long unitCostCents)
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        using (var insert = transaction.Command(
                   """
                   INSERT INTO stock_movements
                       (id, tenant_id, store_id, inventory_item_id, qty_mg, movement_type, reference_type,
                        reference_id, unit_cost_cents, created_at, origin_device_id, client_uuid, is_synced)
                   VALUES ($id, $tenant, $store, $inventory, $qty, $type, $reference, $ref, $cost, $now,
                           $device, $uuid, 0)
                   """,
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
                   ("$inventory", inventoryItemId), ("$qty", quantity), ("$type", movementType),
                   ("$reference", referenceType), ("$ref", orderItemId), ("$cost", unitCostCents), ("$now", now),
                   ("$device", terminal.DeviceId), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        using (var balance = transaction.Command(
                   "UPDATE inventory_items SET balance_mg = balance_mg + $qty, updated_at = $now WHERE id = $id",
                   ("$qty", quantity), ("$now", now), ("$id", inventoryItemId)))
        {
            balance.ExecuteNonQuery();
        }

        _outbox.Enqueue(transaction, "stock_movements", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["tenant_id"] = terminal.TenantId,
            ["store_id"] = terminal.StoreId,
            ["inventory_item_id"] = inventoryItemId,
            ["qty_mg"] = quantity,
            ["movement_type"] = movementType,
            ["reference_type"] = referenceType,
            ["reference_id"] = orderItemId,
            ["unit_cost_cents"] = unitCostCents,
            ["created_at"] = now,
            ["origin_device_id"] = terminal.DeviceId,
            ["client_uuid"] = clientUuid,
        });
    }
}
