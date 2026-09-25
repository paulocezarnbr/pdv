using System.Globalization;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Scale;
using Pdv.Core.Stock;

namespace Pdv.Data.Sales;

public sealed record SaleItem(
    string Id, string ProductId, string ProductName, decimal Quantity, long UnitPriceCents, long TotalCents,
    IReadOnlyList<IngredientConsumption> Consumptions,
    string PricingMode = "unit", long GrossWeightGrams = 0, long TareGrams = 0, long NetWeightGrams = 0,
    string? ScaleReadingRaw = null)
{
    public bool IsWeighed => PricingMode == "weight";
}

public sealed record ItemResult(SaleItem Item, IReadOnlyList<string> StockWarnings, OpenOrder Order);

public sealed class InsufficientStockException(string message) : Exception(message);

/// <summary>Peso não estabilizado: a mercadoria ainda está assentando no prato.</summary>
public sealed class UnstableWeightException(string message) : Exception(message);

/// <summary>Produto pesável sem ficha técnica: não há como dar baixa certa.</summary>
public sealed class RecipeNotFoundException(string message) : Exception(message);

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

            return new ItemResult(item, warnings, SaleRepository.RecomputeTotals(transaction, order.Id, _clock));
        });
    }

    /// <summary>Registra um item pesado — o <c>register_weighed_item</c> do Python, numa transação só.</summary>
    /// <remarks>
    /// <para>
    /// O quadro <b>cru</b> da balança vai para o item e para a auditoria: é a
    /// prova de que o peso cobrado foi o peso lido.
    /// </para>
    /// <para>
    /// Dois eventos de propósito: <c>weight_captured</c> é o que a balança disse,
    /// <c>item_registered</c> é o que foi cobrado. Divergência entre os dois
    /// denuncia manipulação.
    /// </para>
    /// <para>
    /// Diferente do Python, nada é gravado antes das validações: lá o pedido
    /// abria numa transação própria e ficava vazio quando a tara era maior que o
    /// peso.
    /// </para>
    /// </remarks>
    /// <exception cref="UnstableWeightException">A balança não disse "estável".</exception>
    /// <exception cref="InvalidQuantityException">Produto por unidade, peso zerado ou tara maior que o bruto.</exception>
    /// <exception cref="RecipeNotFoundException">Pesável sem ficha técnica.</exception>
    public ItemResult RegisterWeighedItem(
        string? orderId, Product product, ScaleReading reading, string operatorId, long? tareOverrideGrams = null)
    {
        if (!reading.Sellable)
        {
            throw new UnstableWeightException(
                $"Peso não estabilizado (status: {reading.StatusName}). Aguarde a balança parar antes de registrar.");
        }
        if (!product.IsWeighed) throw new InvalidQuantityException($"Produto '{product.Name}' não é vendido por peso");

        var gross = reading.WeightGrams;
        var tare = tareOverrideGrams ?? product.TareGrams;
        var net = WeightPricing.NetWeight(gross, tare);
        if (net <= 0) throw new InvalidQuantityException("Peso líquido zerado após descontar a tara");
        var total = WeightPricing.PriceForWeight(product.PriceCents, net);

        var recipe = new Catalog(database.Connection, terminal.TenantId).RecipeFor(product)
                     ?? throw new RecipeNotFoundException(product.RecipeId is null
                         ? $"Produto '{product.Name}' não possui ficha técnica cadastrada"
                         : $"Ficha técnica {product.RecipeId} não encontrada no banco local");
        var consumptions = RecipeExplosion.Explode(recipe, net);

        return database.InTransaction(transaction =>
        {
            var order = orderId is null
                ? _sales.CreateOrder(transaction, operatorId)
                : SaleRepository.LoadOpenOrder(transaction, orderId);

            var warnings = CheckAvailability(transaction, consumptions);
            var item = new SaleItem(
                Iso.NewId(), product.Id, product.Name, 1m, product.PriceCents, total, consumptions,
                "weight", gross, tare, net, reading.RawFrame);
            AddItem(transaction, order.Id, item, product, operatorId);
            foreach (var consumption in consumptions)
            {
                WriteOff(transaction, consumption, item.Id);
            }

            ledger.Append(transaction, "weight_captured", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = order.Id,
                ["product_id"] = product.Id,
                ["product_name"] = product.Name,
                ["gross_grams"] = gross,
                ["tare_grams"] = tare,
                ["net_grams"] = net,
                ["scale_status"] = reading.StatusName,
                ["scale_raw_frame"] = reading.RawFrame,
                // datetime.isoformat() do Python: microssegundos, não o iso() de milissegundos.
                ["read_at"] = reading.ReadAt.ToUniversalTime().ToString(
                    "yyyy-MM-dd'T'HH:mm:ss.ffffff'+00:00'", CultureInfo.InvariantCulture),
            });
            ledger.Append(transaction, "item_registered", operatorId, new Dictionary<string, object?>
            {
                ["order_id"] = order.Id,
                ["order_item_id"] = item.Id,
                ["product_id"] = product.Id,
                ["net_grams"] = net,
                ["price_cents_per_kg"] = product.PriceCents,
                ["total_cents"] = total,
                ["consumptions"] = consumptions.Select(consumption => new Dictionary<string, object?>
                {
                    ["inventory_item_id"] = consumption.InventoryItemId,
                    ["consumed_mg"] = consumption.ConsumedMg,
                }).ToList(),
            });

            return new ItemResult(item, warnings, SaleRepository.RecomputeTotals(transaction, order.Id, _clock));
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
                   VALUES ($id, $order, $tenant, $product, $name, $mode, $qty, $gross, $tare, $net, $price, $total,
                           $raw, $now, $by, $uuid, 0)
                   """,
                   ("$id", item.Id), ("$order", orderId), ("$tenant", terminal.TenantId), ("$product", product.Id),
                   ("$name", product.Name), ("$mode", item.PricingMode), ("$qty", PythonDecimal(item.Quantity)),
                   ("$gross", item.GrossWeightGrams), ("$tare", item.TareGrams), ("$net", item.NetWeightGrams),
                   ("$price", item.UnitPriceCents), ("$total", item.TotalCents), ("$raw", item.ScaleReadingRaw),
                   ("$now", now), ("$by", operatorId), ("$uuid", clientUuid)))
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
            ["pricing_mode"] = item.PricingMode,
            ["quantity"] = PythonDecimal(item.Quantity),
            ["gross_weight_grams"] = item.GrossWeightGrams,
            ["tare_grams"] = item.TareGrams,
            ["net_weight_grams"] = item.NetWeightGrams,
            ["unit_price_cents"] = item.UnitPriceCents,
            ["total_cents"] = item.TotalCents,
            // Prova pericial: o quadro cru sobe para a nuvem junto do item.
            ["scale_reading_raw"] = item.ScaleReadingRaw,
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

    /// <summary>O <c>str(Decimal)</c> do Python: "2", "1.5", "0.25" — quantidade vai como texto.</summary>
    private static string PythonDecimal(decimal value) => value.ToString(CultureInfo.InvariantCulture);
}
