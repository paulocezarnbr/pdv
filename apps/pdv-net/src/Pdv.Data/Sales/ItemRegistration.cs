using System.Globalization;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Stock;

namespace Pdv.Data.Sales;

public sealed record SaleItem(
    string Id, string ProductId, string ProductName, decimal Quantity, long UnitPriceCents, long TotalCents,
    IReadOnlyList<IngredientConsumption> Consumptions);

public sealed record ItemResult(SaleItem Item, IReadOnlyList<string> StockWarnings, OpenOrder Order);

public sealed class InsufficientStockException(string message) : Exception(message);

/// <summary>O item da venda — o <c>register_unit_item</c> do Python, numa transação só.</summary>
/// <remarks>
/// <para>
/// Uma transação para o item, a baixa de estoque, o consumo por insumo e a
/// auditoria: não existe item cobrado sem baixa, nem baixa sem item.
/// </para>
/// <para>
/// Estoque negativo <b>avisa e não trava a fila</b> (padrão do Python): quase
/// sempre é cadastro errado ou nota de entrada atrasada, e travar a venda custa
/// mais que o insumo. A loja que prefere travar liga
/// <c>blockSaleOnNegativeStock</c>.
/// </para>
/// </remarks>
public sealed class ItemRegistration(
    PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger,
    bool blockSaleOnNegativeStock = false, TimeProvider? clock = null)
{
    private readonly TimeProvider _clock = clock ?? TimeProvider.System;
    private readonly Outbox _outbox = new(clock);
    private readonly SaleRepository _sales = new(terminal, clock);

    /// <summary>Registra um item por unidade. Sem pedido aberto, abre um (já gravado).</summary>
    public ItemResult RegisterUnitItem(string? orderId, Product product, decimal quantity, string operatorId)
    {
        if (product.IsWeighed)
        {
            throw new InvalidQuantityException($"Produto '{product.Name}' é vendido por peso — use a balança.");
        }
        if (quantity <= 0) throw new InvalidQuantityException("Quantidade precisa ser maior que zero.");

        // Arredondamento único no fim: por parcela acumularia centavos no dia.
        var total = RecipeExplosion.UnitTotal(product.PriceCents, quantity);
        var recipe = new Catalog(database.Connection, terminal.TenantId).RecipeFor(product);
        var consumptions = recipe is null
            ? []
            : RecipeExplosion.Explode(recipe, RecipeExplosion.UnitPortionGrams(recipe.BaseQtyG, quantity));

        return database.InTransaction(transaction =>
        {
            var order = orderId is null
                ? _sales.CreateOrder(transaction, operatorId)
                : SaleRepository.LoadOpenOrder(transaction, orderId);

            var warnings = CheckAvailability(transaction, consumptions);
            var item = new SaleItem(Iso.NewId(), product.Id, product.Name, quantity, product.PriceCents, total, consumptions);
            AddItem(transaction, order.Id, item, product, operatorId);
            foreach (var consumption in consumptions)
            {
                WriteOff(transaction, consumption, item.Id);
            }

            ledger.Append(transaction, "item_registered", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = order.Id,
                ["order_item_id"] = item.Id,
                ["product_id"] = product.Id,
                ["quantity"] = PythonDecimal(quantity),
                ["unit_price_cents"] = product.PriceCents,
                ["total_cents"] = total,
            });

            return new ItemResult(item, warnings, UpdateTotals(transaction, order));
        });
    }

    private List<string> CheckAvailability(SqliteTransaction transaction, IReadOnlyList<IngredientConsumption> consumptions)
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

    private void AddItem(SqliteTransaction transaction, string orderId, SaleItem item, Product product, string operatorId)
    {
        var now = Iso.Now(_clock);
        var clientUuid = Iso.NewId();
        using (var insert = transaction.Command(
                   """
                   INSERT INTO order_items
                       (id, order_id, tenant_id, product_id, product_name, pricing_mode, quantity,
                        gross_weight_grams, tare_grams, net_weight_grams, unit_price_cents, total_cents,
                        scale_reading_raw, created_at, created_by_user_id, client_uuid, is_synced)
                   VALUES ($id, $order, $tenant, $product, $name, 'unit', $qty, 0, 0, 0, $price, $total,
                           NULL, $now, $by, $uuid, 0)
                   """,
                   ("$id", item.Id), ("$order", orderId), ("$tenant", terminal.TenantId), ("$product", product.Id),
                   ("$name", product.Name), ("$qty", PythonDecimal(item.Quantity)), ("$price", item.UnitPriceCents),
                   ("$total", item.TotalCents), ("$now", now), ("$by", operatorId), ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }

        // Os ids vão no payload: a nuvem grava com o MESMO id e client_uuid, e
        // um reenvio cai no ON CONFLICT em vez de virar um segundo consumo.
        var ingredients = new List<Dictionary<string, object?>>();
        foreach (var consumption in item.Consumptions)
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
                       ("$id", ingredientId), ("$item", item.Id), ("$inventory", consumption.InventoryItemId),
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

        _outbox.Enqueue(transaction, "order_items", item.Id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = item.Id,
            ["order_id"] = orderId,
            ["tenant_id"] = terminal.TenantId,
            ["product_id"] = product.Id,
            ["product_name"] = product.Name,
            ["pricing_mode"] = "unit",
            ["quantity"] = PythonDecimal(item.Quantity),
            ["gross_weight_grams"] = 0,
            ["tare_grams"] = 0,
            ["net_weight_grams"] = 0,
            ["unit_price_cents"] = item.UnitPriceCents,
            ["total_cents"] = item.TotalCents,
            ["scale_reading_raw"] = null,
            ["created_at"] = now,
            ["client_uuid"] = clientUuid,
            ["created_by_user_id"] = operatorId,
            ["ingredients"] = ingredients,
        });
    }

    /// <summary>Movimento de saída (quantidade negativa) e o cache de saldo, como o <c>register_movement</c>.</summary>
    private void WriteOff(SqliteTransaction transaction, IngredientConsumption consumption, string orderItemId)
    {
        var id = Iso.NewId();
        var clientUuid = Iso.NewId();
        var now = Iso.Now(_clock);
        var quantity = -consumption.ConsumedMg;
        using (var insert = transaction.Command(
                   """
                   INSERT INTO stock_movements
                       (id, tenant_id, store_id, inventory_item_id, qty_mg, movement_type, reference_type,
                        reference_id, unit_cost_cents, created_at, origin_device_id, client_uuid, is_synced)
                   VALUES ($id, $tenant, $store, $inventory, $qty, 'sale', 'order_item', $ref, $cost, $now,
                           $device, $uuid, 0)
                   """,
                   ("$id", id), ("$tenant", terminal.TenantId), ("$store", terminal.StoreId),
                   ("$inventory", consumption.InventoryItemId), ("$qty", quantity), ("$ref", orderItemId),
                   ("$cost", consumption.UnitCostCents), ("$now", now), ("$device", terminal.DeviceId),
                   ("$uuid", clientUuid)))
        {
            insert.ExecuteNonQuery();
        }
        using (var balance = transaction.Command(
                   "UPDATE inventory_items SET balance_mg = balance_mg + $qty, updated_at = $now WHERE id = $id",
                   ("$qty", quantity), ("$now", now), ("$id", consumption.InventoryItemId)))
        {
            balance.ExecuteNonQuery();
        }

        _outbox.Enqueue(transaction, "stock_movements", id, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = id,
            ["tenant_id"] = terminal.TenantId,
            ["store_id"] = terminal.StoreId,
            ["inventory_item_id"] = consumption.InventoryItemId,
            ["qty_mg"] = quantity,
            ["movement_type"] = "sale",
            ["reference_type"] = "order_item",
            ["reference_id"] = orderItemId,
            ["unit_cost_cents"] = consumption.UnitCostCents,
            ["created_at"] = now,
            ["origin_device_id"] = terminal.DeviceId,
            ["client_uuid"] = clientUuid,
        });
    }

    /// <summary>Totais a partir dos itens vivos no banco — a fonte da verdade, não a memória da tela.</summary>
    private OpenOrder UpdateTotals(SqliteTransaction transaction, OpenOrder order)
    {
        long subtotal, discount;
        using (var sums = transaction.Command(
                   "SELECT COALESCE(SUM(total_cents), 0), (SELECT discount_cents FROM orders WHERE id = $order) " +
                   "FROM order_items WHERE order_id = $order AND canceled_at IS NULL",
                   ("$order", order.Id)))
        using (var reader = sums.ExecuteReader())
        {
            reader.Read();
            subtotal = reader.GetInt64(0);
            discount = reader.IsDBNull(1) ? 0 : reader.GetInt64(1);
        }
        var total = subtotal - discount;
        using (var update = transaction.Command(
                   "UPDATE orders SET subtotal_cents = $sub, discount_cents = $disc, total_cents = $total, " +
                   "updated_at = $now WHERE id = $id",
                   ("$sub", subtotal), ("$disc", discount), ("$total", total), ("$now", Iso.Now(_clock)),
                   ("$id", order.Id)))
        {
            update.ExecuteNonQuery();
        }
        return order with { SubtotalCents = subtotal, DiscountCents = discount, TotalCents = total };
    }

    /// <summary>O <c>str(Decimal)</c> do Python: "2", "1.5", "0.25" — quantidade vai como texto.</summary>
    private static string PythonDecimal(decimal value) => value.ToString(CultureInfo.InvariantCulture);
}
