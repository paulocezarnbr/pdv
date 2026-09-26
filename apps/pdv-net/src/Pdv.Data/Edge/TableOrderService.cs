using System.Globalization;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;
using Pdv.Core;
using Pdv.Core.Stock;
using Pdv.Data.Sales;

namespace Pdv.Data.Edge;

public sealed class OrderNotFoundException(string message) : Exception(message);

public sealed class OrderClosedException(string message) : Exception(message);

public sealed class ProductNotSellableException(string message) : Exception(message);

/// <summary>A mesa já tem comanda aberta — e a resposta certa para o garçom é essa comanda.</summary>
public sealed class TableOccupiedException(string message, TableOrder order) : Exception(message)
{
    public TableOrder Order { get; } = order;
}

/// <summary>A comanda de mesa, como o app do garçom e o caixa a leem.</summary>
public sealed record TableOrder(
    string Id,
    string ClientUuid,
    long LocalNumber,
    string TableLabel,
    string Status,
    long TotalCents,
    int ItemCount,
    string? TableId = null,
    string? BillRequestedAt = null,
    string? OperatorId = null,
    string WaiterName = "",
    long TipCents = 0,
    string? OpenedAt = null,
    long SubtotalCents = 0,
    long DiscountCents = 0)
{
    public bool BillRequested => !string.IsNullOrEmpty(BillRequestedAt);

    /// <summary>O que o cliente paga: a conta mais a gorjeta.</summary>
    public long ChargedCents => TotalCents + TipCents;

    public JsonObject ToJson() => new()
    {
        ["order_id"] = Id,
        ["client_uuid"] = ClientUuid,
        ["local_number"] = LocalNumber,
        ["table_id"] = TableId,
        ["table_label"] = TableLabel,
        ["status"] = Status,
        ["total_cents"] = TotalCents,
        ["tip_cents"] = TipCents,
        ["item_count"] = ItemCount,
        ["bill_requested_at"] = BillRequestedAt,
        ["operator_id"] = OperatorId,
        ["waiter_name"] = WaiterName,
        ["opened_at"] = OpenedAt,
    };
}

/// <summary>O recebimento da mesa: a comanda fechada e as formas de pagamento, com o troco.</summary>
public sealed record SettledOrder(TableOrder Order, IReadOnlyList<Payment> Payments, long TipCents, long ChargedCents)
{
    public long ChangeCents => Payments.Sum(payment => payment.ChangeCents);
}

/// <summary>
/// As comandas do salão — o <c>edge/orders.py</c>, recusa a recusa.
/// </summary>
/// <remarks>
/// <para>
/// <b>O <c>client_uuid</c> do celular é gravado como veio.</b> O app tem dois
/// caminhos até a nuvem (a LAN e a internet) e não sabe, sob falha, se o outro
/// já entregou; um uuid gerado aqui faria o mesmo pedido chegar com dois
/// identificadores, e a loja seria cobrada duas vezes. Pelo mesmo motivo,
/// reenviar é inofensivo: o uuid já visto devolve o pedido existente.
/// </para>
/// <para>
/// <b>O celular pede a conta; quem recebe é o caixa.</b> Um segundo ponto de
/// recebimento, sem gaveta nem conferência de troco, é como o furto de sala
/// entra pela porta da frente. Dividir e juntar também são do caixa.
/// </para>
/// <para>Conferido contra <c>contracts/salon.json</c>.</para>
/// </remarks>
public sealed class TableOrderService
{
    private readonly PdvDatabase _database;
    private readonly TerminalIdentity _terminal;
    private readonly AuditLedger _ledger;
    private readonly EventHub _hub;
    private readonly TimeProvider _clock;
    private readonly Outbox _outbox;
    private readonly SaleRepository _sales;
    private readonly StockWriter _stock;
    private readonly bool _blockSaleOnNegativeStock;

    public TableOrderService(
        PdvDatabase database, TerminalIdentity terminal, AuditLedger ledger, EventHub? hub = null, TimeProvider? clock = null,
        bool blockSaleOnNegativeStock = false)
    {
        _blockSaleOnNegativeStock = blockSaleOnNegativeStock;
        _stock = new StockWriter(terminal, clock);
        _database = database;
        _terminal = terminal;
        _ledger = ledger;
        _clock = clock ?? TimeProvider.System;
        _hub = hub ?? new EventHub(_clock);
        _outbox = new Outbox(_clock);
        _sales = new SaleRepository(terminal, _clock);
    }

    // -- abertura -------------------------------------------------------------

    /// <summary>Abre a comanda, ou devolve a existente se o uuid já foi visto.</summary>
    /// <exception cref="TableOccupiedException">A mesa já tem comanda aberta; a exceção carrega essa comanda.</exception>
    public TableOrder OpenOrder(string clientUuid, string operatorId, string originDeviceId, string? tableId = null, string tableLabel = "")
    {
        if (FindByClientUuid(clientUuid) is { } existing) return existing;

        var table = ResolveTable(tableId, tableLabel);
        if (OpenOrderOf(table.Id) is { } occupying)
        {
            throw new TableOccupiedException($"A {table.Label} já tem a comanda {occupying.LocalNumber} aberta.", occupying);
        }

        var orderId = Iso.NewId();
        var now = Iso.Now(_clock);
        long localNumber;
        try
        {
            localNumber = _database.InTransaction(transaction =>
            {
                var number = SaleRepository.NextCounter(transaction, "order_local_number");
                InsertTableOrder(transaction, orderId, clientUuid, operatorId, number, table.Id, table.Label, originDeviceId, now);
                return number;
            });
        }
        catch (SqliteException error) when (error.SqliteErrorCode == 19)
        {
            // Dois reenvios simultâneos do mesmo celular: o outro ganhou, e a resposta certa é o pedido dele.
            return FindByClientUuid(clientUuid) ?? throw new InvalidOperationException("Pedido sumiu na corrida.", error);
        }

        _hub.Publish("order.opened", new JsonObject
        {
            ["order_id"] = orderId, ["local_number"] = localNumber, ["table_id"] = table.Id, ["table_label"] = table.Label,
        });
        return new TableOrder(orderId, clientUuid, localNumber, table.Label, "open", 0, 0, table.Id);
    }

    // -- conta ----------------------------------------------------------------

    /// <summary>O garçom pede a conta. Não recebe. Repetir não muda o instante gravado.</summary>
    public TableOrder RequestBill(string orderId)
    {
        var order = RequireOpen(orderId);
        if (order.BillRequested) return order;

        var now = Iso.Now(_clock);
        _database.InTransaction(transaction =>
        {
            transaction.Command(
                "UPDATE orders SET bill_requested_at = $now, updated_at = $now, is_synced = 0 WHERE id = $id AND status = 'open'",
                ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", orderId, Iso.NewId(), "update",
                new Dictionary<string, object?> { ["id"] = orderId, ["bill_requested_at"] = now });
        });
        _hub.Publish("order.bill_requested", new JsonObject
        {
            ["order_id"] = orderId, ["local_number"] = order.LocalNumber, ["table_label"] = order.TableLabel,
            ["total_cents"] = order.TotalCents,
        });
        return GetOrder(orderId);
    }

    /// <summary>Desfaz o pedido de conta — a mesa resolveu pedir sobremesa.</summary>
    public TableOrder ClearBillRequest(string orderId)
    {
        var order = RequireOpen(orderId);
        if (!order.BillRequested) return order;

        var now = Iso.Now(_clock);
        _database.InTransaction(transaction =>
        {
            transaction.Command(
                "UPDATE orders SET bill_requested_at = NULL, updated_at = $now, is_synced = 0 WHERE id = $id AND status = 'open'",
                ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", orderId, Iso.NewId(), "update",
                new Dictionary<string, object?> { ["id"] = orderId, ["bill_requested_at"] = null });
        });
        _hub.Publish("order.bill_cleared", new JsonObject { ["order_id"] = orderId });
        return GetOrder(orderId);
    }

    /// <summary>O caixa recebe a conta da mesa, com a gorjeta por fora do total.</summary>
    public SettledOrder Settle(string orderId, IReadOnlyList<PaymentIntent> intents, string operatorId, string operatorName, long tipCents = 0)
    {
        var order = RequireOpen(orderId);
        if (order.ItemCount == 0)
        {
            throw new OrderClosedException(
                $"A comanda {order.LocalNumber} não tem itens. Mesa sem consumo se libera cancelando, não recebendo.");
        }
        var tip = Math.Max(0, tipCents);
        var charged = order.TotalCents + tip;
        var payments = Payments(intents, charged);

        _database.InTransaction(transaction => ClosePaid(transaction, order, payments, tip, operatorId, operatorName));
        _hub.Publish("order.settled", new JsonObject
        {
            ["order_id"] = orderId, ["local_number"] = order.LocalNumber, ["table_id"] = order.TableId,
            ["table_label"] = order.TableLabel, ["total_cents"] = order.TotalCents, ["tip_cents"] = tip,
        });
        return new SettledOrder(GetOrder(orderId), payments, tip, charged);
    }

    /// <summary>Cancela a comanda inteira — exige gerente, e vira evento crítico no ledger.</summary>
    public TableOrder CancelOrder(string orderId, string authorizerId, string authorizerName, string reason)
    {
        var order = RequireOpen(orderId);
        reason = new string(string.Join(' ', reason.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
            .EnumerateRunes().Take(200).SelectMany(rune => rune.ToString()).ToArray());
        if (reason.Length == 0) throw new ProductNotSellableException("Cancelar comanda exige motivo.");

        var now = Iso.Now(_clock);
        _database.InTransaction(transaction =>
        {
            var live = new List<string>();
            using (var items = transaction.Command(
                       "SELECT id FROM order_items WHERE order_id = $id AND canceled_at IS NULL", ("$id", orderId)))
            using (var reader = items.ExecuteReader())
            {
                while (reader.Read()) live.Add(reader.GetString(0));
            }
            foreach (var itemId in live)
            {
                // O insumo baixou no lançamento; cancelar a comanda o devolve.
                if (CancelItem(transaction, itemId, now, authorizerId, $"[comanda cancelada] {reason}"))
                {
                    _stock.RestoreItem(transaction, itemId);
                }
            }
            // Ticket de comanda cancelada sai da tela: manter é mandar preparar comida que ninguém vai receber.
            transaction.Command(
                "UPDATE kds_tickets SET status = 'canceled', updated_at = $now WHERE order_id = $id AND status <> 'canceled'",
                ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            transaction.Command(
                "UPDATE orders SET status = 'canceled', closed_at = $now, authorized_by_user_id = $by, subtotal_cents = 0, " +
                "discount_cents = 0, total_cents = 0, updated_at = $now, is_synced = 0 WHERE id = $id",
                ("$now", now), ("$by", authorizerId), ("$id", orderId)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", orderId, Iso.NewId(), "update", new Dictionary<string, object?>
            {
                ["id"] = orderId, ["status"] = "canceled", ["closed_at"] = now, ["subtotal_cents"] = 0,
                ["discount_cents"] = 0, ["total_cents"] = 0, ["authorized_by_user_id"] = authorizerId, ["reason"] = reason,
            });
            _ledger.Append(transaction, "item_canceled", authorizerId, new Dictionary<string, object?>
            {
                ["order_id"] = orderId, ["local_number"] = order.LocalNumber, ["table_label"] = order.TableLabel,
                ["item_count"] = order.ItemCount, ["total_cents"] = order.TotalCents, ["reason"] = reason,
                ["authorizer_name"] = authorizerName, ["channel"] = "waiter",
            }, severity: "critical", authorizerUserId: authorizerId);
        });
        _hub.Publish("order.canceled", new JsonObject
        {
            ["order_id"] = orderId, ["local_number"] = order.LocalNumber, ["table_label"] = order.TableLabel,
        });
        return GetOrder(orderId);
    }

    /// <summary>Muda a comanda de mesa. O destino precisa estar livre: juntar contas é do caixa.</summary>
    public TableOrder Transfer(string orderId, string tableId, string authorizerId, string authorizerName)
    {
        var order = RequireOpen(orderId);
        var table = ResolveTable(tableId, "");
        if (table.Id == order.TableId) return order;
        if (OpenOrderOf(table.Id) is { } occupying)
        {
            throw new TableOccupiedException($"A {table.Label} já tem a comanda {occupying.LocalNumber} aberta.", occupying);
        }

        var now = Iso.Now(_clock);
        _database.InTransaction(transaction =>
        {
            transaction.Command(
                "UPDATE orders SET table_id = $table, customer_id = $label, updated_at = $now, is_synced = 0 WHERE id = $id",
                ("$table", table.Id), ("$label", table.Label), ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", orderId, Iso.NewId(), "update", new Dictionary<string, object?>
            {
                ["id"] = orderId, ["table_id"] = table.Id, ["customer_id"] = table.Label,
            });
            _ledger.Append(transaction, "price_override", authorizerId, new Dictionary<string, object?>
            {
                ["operation"] = "table_transfer", ["order_id"] = orderId, ["from_table"] = order.TableLabel,
                ["to_table"] = table.Label, ["authorizer_name"] = authorizerName, ["channel"] = "waiter",
            }, severity: "warning", authorizerUserId: authorizerId);
        });
        _hub.Publish("order.transferred", new JsonObject
        {
            ["order_id"] = orderId, ["from_table"] = order.TableLabel, ["to_table"] = table.Label,
        });
        return GetOrder(orderId);
    }

    // -- dividir e juntar (caixa) ----------------------------------------------

    /// <summary>Passa itens de uma comanda aberta para outra. O dinheiro não some nem aparece.</summary>
    public (TableOrder Source, TableOrder Target) MoveItems(
        string sourceOrderId, string targetOrderId, IReadOnlyList<string> itemIds, string operatorId, string operatorName)
    {
        var (source, target) = TwoOpen(sourceOrderId, targetOrderId);
        var ids = LiveItems(source, itemIds);
        var moved = 0L;
        _database.InTransaction(transaction =>
        {
            moved = Move(transaction, ids, source.Id, target.Id);
            _ledger.Append(transaction, "items_transferred", operatorId, new Dictionary<string, object?>
            {
                ["from_order_id"] = source.Id, ["from_local_number"] = source.LocalNumber, ["from_table"] = source.TableLabel,
                ["to_order_id"] = target.Id, ["to_local_number"] = target.LocalNumber, ["to_table"] = target.TableLabel,
                ["item_ids"] = ids.ToList(), ["total_cents"] = moved, ["operator_name"] = operatorName,
            }, severity: "warning");
        });
        PublishMove(source, target, ids.Count, moved);
        return (GetOrder(source.Id), GetOrder(target.Id));
    }

    /// <summary>Junta a origem no destino e libera a mesa de origem. No ledger é junção, não cancelamento.</summary>
    public TableOrder MergeOrders(string sourceOrderId, string targetOrderId, string operatorId, string operatorName)
    {
        var (source, target) = TwoOpen(sourceOrderId, targetOrderId);
        var live = new List<string>();
        using (var items = Sql.Command(_database.Connection, null,
                   "SELECT id FROM order_items WHERE order_id = $id AND canceled_at IS NULL", ("$id", source.Id)))
        using (var reader = items.ExecuteReader())
        {
            while (reader.Read()) live.Add(reader.GetString(0));
        }

        var now = Iso.Now(_clock);
        var moved = 0L;
        _database.InTransaction(transaction =>
        {
            moved = live.Count > 0 ? Move(transaction, live, source.Id, target.Id) : 0;
            transaction.Command(
                "UPDATE orders SET status = 'canceled', closed_at = $now, subtotal_cents = 0, discount_cents = 0, " +
                "total_cents = 0, bill_requested_at = NULL, updated_at = $now, is_synced = 0 WHERE id = $id AND status = 'open'",
                ("$now", now), ("$id", source.Id)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", source.Id, Iso.NewId(), "update", new Dictionary<string, object?>
            {
                ["id"] = source.Id, ["status"] = "canceled", ["closed_at"] = now, ["subtotal_cents"] = 0,
                ["discount_cents"] = 0, ["total_cents"] = 0, ["bill_requested_at"] = null,
            });
            _ledger.Append(transaction, "order_merged", operatorId, new Dictionary<string, object?>
            {
                ["from_order_id"] = source.Id, ["from_local_number"] = source.LocalNumber, ["from_table"] = source.TableLabel,
                ["to_order_id"] = target.Id, ["to_local_number"] = target.LocalNumber, ["to_table"] = target.TableLabel,
                ["items"] = live.Count, ["total_cents"] = moved, ["operator_name"] = operatorName,
            }, severity: "warning");
        });
        PublishMove(source, target, live.Count, moved);
        _hub.Publish("order.merged", new JsonObject
        {
            ["order_id"] = source.Id, ["into_order_id"] = target.Id, ["from_table"] = source.TableLabel,
            ["to_table"] = target.TableLabel,
        });
        return GetOrder(target.Id);
    }

    /// <summary>Recebe parte da conta: os itens escolhidos vão para uma comanda nova, que nasce e é paga na mesma transação.</summary>
    public SettledOrder SettleItems(
        string orderId, IReadOnlyList<string> itemIds, IReadOnlyList<PaymentIntent> intents, string operatorId,
        string operatorName, long tipCents = 0)
    {
        var order = RequireOpen(orderId);
        var ids = LiveItems(order, itemIds);
        var liveTotal = Convert.ToInt64(_database.Scalar(
            "SELECT COUNT(*) FROM order_items WHERE order_id = $id AND canceled_at IS NULL", ("$id", order.Id)));
        if (ids.Count == liveTotal) return Settle(order.Id, intents, operatorId, operatorName, tipCents);

        var part = ItemsTotal(ids);
        var tip = Math.Max(0, tipCents);
        var charged = part + tip;
        var payments = Payments(intents, charged);

        var childId = Iso.NewId();
        _database.InTransaction(transaction =>
        {
            var number = SaleRepository.NextCounter(transaction, "order_local_number");
            // A venda é de quem atendeu a mesa, não de quem recebeu.
            InsertTableOrder(transaction, childId, Iso.NewId(), order.OperatorId ?? operatorId, number, order.TableId,
                order.TableLabel, _terminal.DeviceId, Iso.Now(_clock));
            Move(transaction, ids, order.Id, childId);
            var child = GetOrder(transaction, childId);
            ClosePaid(transaction, child, payments, tip, operatorId, operatorName, splitFrom: order);
        });
        _hub.Publish("order.split_paid", new JsonObject
        {
            ["order_id"] = order.Id, ["part_order_id"] = childId, ["table_label"] = order.TableLabel,
            ["total_cents"] = part, ["tip_cents"] = tip,
        });
        return new SettledOrder(GetOrder(childId), payments, tip, charged);
    }

    // -- itens ----------------------------------------------------------------

    /// <summary>Acrescenta um item unitário e enfileira o ticket da cozinha. Item por peso não entra pelo celular.</summary>
    /// <remarks>
    /// O insumo baixa aqui, no lançamento, pela mesma ficha do balcão: é quando o
    /// prato vai para a cozinha. Quem cancela a comanda estorna. Até a 1.1.5 a
    /// mesa não baixava nada, e o CMV do painel só enxergava o balcão.
    /// </remarks>
    /// <exception cref="ProductNotSellableException">
    /// Produto inexistente, por peso, quantidade inválida, ou sem saldo de insumo com a loja bloqueando.
    /// </exception>
    public TableOrder AddItem(
        string orderId, string clientUuid, string productId, decimal quantity, string notes = "", string station = "cozinha",
        string? createdByUserId = null)
    {
        if (quantity <= 0) throw new ProductNotSellableException("Quantidade precisa ser maior que zero.");

        if (_database.Scalar("SELECT order_id FROM order_items WHERE client_uuid = $uuid", ("$uuid", clientUuid)) is string known)
        {
            return GetOrder(known);
        }

        var order = RequireOpen(orderId);
        var product = new Catalog(_database.Connection, _terminal.TenantId).Get(productId)
                      ?? throw new ProductNotSellableException($"Produto {productId} não encontrado.");
        if (product.IsWeighed)
        {
            throw new ProductNotSellableException($"{product.Name} é vendido por peso e precisa da balança do balcão.");
        }

        // Um arredondamento só, no fim, e o do Python: quantize com meio-para-par.
        var total = (long)Math.Round(product.PriceCents * quantity, 0, MidpointRounding.ToEven);
        var itemId = Iso.NewId();
        var ticketId = Iso.NewId();
        var now = Iso.Now(_clock);
        var quantityText = quantity.ToString(CultureInfo.InvariantCulture);

        var recipe = new Catalog(_database.Connection, _terminal.TenantId).RecipeFor(product);
        _database.InTransaction(transaction =>
        {
            IReadOnlyList<IngredientConsumption> consumptions = [];
            if (recipe is not null)
            {
                try
                {
                    consumptions = RecipeExplosion.Explode(recipe, RecipeExplosion.UnitPortionGrams(recipe.BaseQtyG, quantity));
                    // Saldo baixo só avisa (o padrão do food service); o aviso é do caixa, não do garçom.
                    _stock.CheckAvailability(transaction, consumptions, _blockSaleOnNegativeStock);
                }
                catch (Exception error) when (error is InsufficientStockException or InvalidQuantityException)
                {
                    // A recusa de produto que o app do garçom já sabe mostrar, em vez de um 500.
                    throw new ProductNotSellableException(error.Message);
                }
            }
            transaction.Command(
                """
                INSERT INTO order_items
                    (id, order_id, tenant_id, product_id, product_name, pricing_mode, quantity,
                     gross_weight_grams, tare_grams, net_weight_grams, unit_price_cents, total_cents,
                     scale_reading_raw, created_at, created_by_user_id, client_uuid, is_synced)
                VALUES ($id, $order, $tenant, $product, $name, 'unit', $qty, 0, 0, 0, $price, $total,
                        NULL, $now, $by, $uuid, 0)
                """,
                ("$id", itemId), ("$order", orderId), ("$tenant", _terminal.TenantId), ("$product", product.Id),
                ("$name", product.Name), ("$qty", quantityText), ("$price", product.PriceCents), ("$total", total),
                ("$now", now), ("$by", createdByUserId), ("$uuid", clientUuid)).ExecuteNonQuery();
            var ingredients = _stock.InsertIngredients(transaction, itemId, consumptions, now);
            _outbox.Enqueue(transaction, "order_items", itemId, clientUuid, "insert", new Dictionary<string, object?>
            {
                ["id"] = itemId, ["order_id"] = orderId, ["tenant_id"] = _terminal.TenantId, ["product_id"] = product.Id,
                ["product_name"] = product.Name, ["pricing_mode"] = "unit", ["quantity"] = quantityText,
                ["gross_weight_grams"] = 0, ["tare_grams"] = 0, ["net_weight_grams"] = 0,
                ["unit_price_cents"] = product.PriceCents, ["total_cents"] = total, ["scale_reading_raw"] = null,
                ["created_at"] = now, ["client_uuid"] = clientUuid, ["created_by_user_id"] = createdByUserId,
                ["ingredients"] = ingredients,
            });
            foreach (var consumption in consumptions) _stock.WriteOff(transaction, consumption, itemId);
            transaction.Command(
                "UPDATE orders SET subtotal_cents = subtotal_cents + $total, total_cents = total_cents + $total, " +
                "updated_at = $now WHERE id = $id",
                ("$total", total), ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            var trimmed = notes.Trim();
            transaction.Command(
                """
                INSERT INTO kds_tickets
                    (id, tenant_id, store_id, order_id, order_item_id, station, product_name, quantity, notes, status,
                     queued_at, created_at, updated_at, origin_device_id, client_uuid)
                VALUES ($id, $tenant, $store, $order, $item, $station, $name, $qty, $notes, 'queued', $now, $now, $now,
                        $device, $uuid)
                """,
                ("$id", ticketId), ("$tenant", _terminal.TenantId), ("$store", _terminal.StoreId), ("$order", orderId),
                ("$item", itemId), ("$station", station), ("$name", product.Name), ("$qty", quantityText),
                ("$notes", trimmed.Length == 0 ? null : trimmed[..Math.Min(200, trimmed.Length)]), ("$now", now),
                ("$device", _terminal.DeviceId), ("$uuid", Iso.NewId())).ExecuteNonQuery();
        });

        _hub.Publish("ticket.queued", new JsonObject
        {
            ["ticket_id"] = ticketId, ["order_id"] = orderId, ["local_number"] = order.LocalNumber,
            ["table_label"] = order.TableLabel, ["station"] = station, ["product_name"] = product.Name,
            ["quantity"] = quantityText, ["notes"] = notes,
        });
        return GetOrder(orderId);
    }

    // -- consultas ------------------------------------------------------------

    private const string Select =
        "SELECT o.id, o.client_uuid, o.local_number, o.customer_id, o.status, o.total_cents, o.tip_cents, o.table_id, " +
        "o.bill_requested_at, o.subtotal_cents, o.discount_cents, o.operator_id, o.opened_at, u.name AS waiter_name, " +
        "(SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id AND i.canceled_at IS NULL) AS items " +
        // LEFT JOIN: comanda de quem saiu do cadastro não pode sumir da tela.
        "FROM orders o LEFT JOIN users u ON u.id = o.operator_id ";

    public TableOrder GetOrder(string orderId) => GetOrder(null, orderId);

    private TableOrder GetOrder(SqliteTransaction? transaction, string orderId) =>
        Query(transaction, Select + "WHERE o.id = $id AND o.tenant_id = $tenant", ("$id", orderId), ("$tenant", _terminal.TenantId))
            .FirstOrDefault() ?? throw new OrderNotFoundException($"Pedido {orderId} não encontrado.");

    public IReadOnlyList<TableOrder> ListOpenOrders() =>
        Query(null, Select + "WHERE o.tenant_id = $tenant AND o.status = 'open' AND o.channel = 'waiter' ORDER BY o.opened_at",
            ("$tenant", _terminal.TenantId));

    /// <summary>Os itens da comanda, cancelados inclusive e marcados: é o que o garçom mostra ao cliente que reclama.</summary>
    public JsonArray ListItems(string orderId)
    {
        using var command = Sql.Command(_database.Connection, null,
            "SELECT i.id, i.product_name, i.quantity, i.unit_price_cents, i.total_cents, i.created_at, i.canceled_at, " +
            "i.cancel_reason, (SELECT t.status FROM kds_tickets t WHERE t.order_item_id = i.id " +
            "ORDER BY t.created_at DESC LIMIT 1) AS kds_status FROM order_items i WHERE i.order_id = $id ORDER BY i.created_at",
            ("$id", orderId));
        using var reader = command.ExecuteReader();
        var items = new JsonArray();
        while (reader.Read())
        {
            items.Add(new JsonObject
            {
                ["id"] = reader.GetString(0),
                ["product_name"] = reader.GetString(1),
                ["quantity"] = reader.GetValue(2).ToString(),
                ["unit_price_cents"] = reader.GetInt64(3),
                ["total_cents"] = reader.GetInt64(4),
                ["created_at"] = reader.GetString(5),
                ["canceled"] = !reader.IsDBNull(6),
                ["cancel_reason"] = reader.IsDBNull(7) ? null : reader.GetString(7),
                ["kds_status"] = reader.IsDBNull(8) ? null : reader.GetString(8),
            });
        }
        return items;
    }

    // -- internos -------------------------------------------------------------

    private void InsertTableOrder(
        SqliteTransaction transaction, string orderId, string clientUuid, string operatorId, long localNumber,
        string? tableId, string tableLabel, string originDeviceId, string now)
    {
        transaction.Command(
            """
            INSERT INTO orders
                (id, tenant_id, store_id, device_id, local_number, channel, status, operator_id, opened_at,
                 created_at, updated_at, origin_device_id, client_uuid, is_synced)
            VALUES ($id, $tenant, $store, $device, $number, 'waiter', 'open', $operator, $now, $now, $now, $origin, $uuid, 0)
            """,
            ("$id", orderId), ("$tenant", _terminal.TenantId), ("$store", _terminal.StoreId), ("$device", _terminal.DeviceId),
            ("$number", localNumber), ("$operator", operatorId), ("$now", now), ("$origin", originDeviceId),
            ("$uuid", clientUuid)).ExecuteNonQuery();
        // `customer_id` guarda a CÓPIA do rótulo: renomear a mesa amanhã não reescreve o cupom de hoje.
        transaction.Command(
            "UPDATE orders SET table_id = $table, customer_id = $label, updated_at = $now WHERE id = $id",
            ("$table", tableId), ("$label", tableLabel), ("$now", now), ("$id", orderId)).ExecuteNonQuery();
        _outbox.Enqueue(transaction, "orders", orderId, clientUuid, "insert", new Dictionary<string, object?>
        {
            ["id"] = orderId, ["channel"] = "waiter", ["status"] = "open", ["table_id"] = tableId,
            ["table_label"] = tableLabel, ["customer_id"] = tableLabel, ["local_number"] = localNumber,
            ["operator_id"] = operatorId, ["opened_at"] = now, ["origin_device_id"] = originDeviceId,
        });
    }

    /// <summary>Fecha como paga, na transação do chamador: recebimento inteiro e parcial gravam o mesmo.</summary>
    private void ClosePaid(
        SqliteTransaction transaction, TableOrder order, IReadOnlyList<Payment> payments, long tip, string operatorId,
        string operatorName, TableOrder? splitFrom = null)
    {
        var now = Iso.Now(_clock);
        transaction.Command(
            "UPDATE orders SET status = 'paid', closed_at = $now, updated_at = $now, tip_cents = $tip, is_synced = 0 " +
            "WHERE id = $id AND status = 'open'",
            ("$now", now), ("$tip", tip), ("$id", order.Id)).ExecuteNonQuery();
        _sales.RecordPayments(transaction, order.Id, payments);
        _outbox.Enqueue(transaction, "orders", order.Id, Iso.NewId(), "update", new Dictionary<string, object?>
        {
            ["id"] = order.Id, ["status"] = "paid", ["closed_at"] = now, ["subtotal_cents"] = order.SubtotalCents,
            ["discount_cents"] = order.DiscountCents, ["total_cents"] = order.TotalCents, ["tip_cents"] = tip,
            ["table_id"] = order.TableId, ["served_by_user_id"] = order.OperatorId,
        });
        var payload = new Dictionary<string, object?>
        {
            ["order_id"] = order.Id, ["local_number"] = order.LocalNumber, ["channel"] = "waiter",
            ["table_label"] = order.TableLabel, ["served_by_user_id"] = order.OperatorId,
            ["served_by_name"] = order.WaiterName, ["received_by_name"] = operatorName, ["items"] = order.ItemCount,
            ["total_cents"] = order.TotalCents, ["tip_cents"] = tip,
            ["payments"] = payments.Select(p => (object?)new Dictionary<string, object?>
            {
                ["method"] = p.Method, ["amount_cents"] = p.AmountCents,
            }).ToList(),
        };
        if (splitFrom is not null)
        {
            payload["split_from_order_id"] = splitFrom.Id;
            payload["split_from_local_number"] = splitFrom.LocalNumber;
        }
        _ledger.Append(transaction, "sale_closed", operatorId, payload);
    }

    /// <summary>O <c>settle_payments</c>: troco só em dinheiro, e no primeiro dinheiro.</summary>
    private static List<Payment> Payments(IReadOnlyList<PaymentIntent> intents, long charged)
    {
        var change = PaymentSettlement.Change(intents, charged);
        var cashIndex = change == 0 ? -1 : intents.ToList().FindIndex(intent => intent.Method == PaymentMethods.Cash);
        return [.. intents.Select((intent, index) =>
            new Payment(intent.Method, intent.AmountCents, index == cashIndex ? change : 0, null, Iso.NewId()))];
    }

    private bool CancelItem(SqliteTransaction transaction, string itemId, string canceledAt, string by, string reason)
    {
        var changed = transaction.Command(
            "UPDATE order_items SET canceled_at = $at, canceled_by_user_id = $by, cancel_reason = $reason " +
            "WHERE id = $id AND canceled_at IS NULL",
            ("$at", canceledAt), ("$by", by), ("$reason", reason), ("$id", itemId)).ExecuteNonQuery();
        if (changed == 0) return false;
        _outbox.Enqueue(transaction, "order_items", itemId, Iso.NewId(), "update", new Dictionary<string, object?>
        {
            ["id"] = itemId, ["canceled_at"] = canceledAt, ["canceled_by_user_id"] = by, ["cancel_reason"] = reason,
        });
        return true;
    }

    private (TableOrder Source, TableOrder Target) TwoOpen(string sourceId, string targetId)
    {
        if (sourceId == targetId) throw new OrderClosedException("Origem e destino são a mesma comanda.");
        var source = RequireOpen(sourceId);
        var target = RequireOpen(targetId);
        foreach (var order in new[] { source, target })
        {
            // Repartir itens de comanda com desconto obrigaria a decidir quanto do desconto vai junto.
            if (order.DiscountCents != 0)
            {
                throw new OrderClosedException(
                    $"A comanda {order.LocalNumber} tem desconto. Remova-o antes de dividir ou juntar.");
            }
        }
        return (source, target);
    }

    private List<string> LiveItems(TableOrder order, IReadOnlyList<string> itemIds)
    {
        var wanted = itemIds.Distinct(StringComparer.Ordinal).ToList();
        if (wanted.Count == 0) throw new OrderClosedException("Escolha ao menos um item.");
        var marks = string.Join(",", wanted.Select((_, i) => $"$i{i}"));
        var found = new HashSet<string>(StringComparer.Ordinal);
        using (var command = Sql.Command(_database.Connection, null,
                   $"SELECT id FROM order_items WHERE order_id = $order AND canceled_at IS NULL AND id IN ({marks})",
                   [("$order", order.Id), .. wanted.Select((id, i) => ($"$i{i}", (object?)id))]))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read()) found.Add(reader.GetString(0));
        }
        var missing = wanted.Count(id => !found.Contains(id));
        if (missing > 0)
        {
            throw new OrderClosedException(
                $"{missing} item(ns) não estão vivos na comanda {order.LocalNumber} — já foram cancelados, pagos ou movidos.");
        }
        return wanted;
    }

    private long ItemsTotal(IReadOnlyList<string> ids)
    {
        var marks = string.Join(",", ids.Select((_, i) => $"$i{i}"));
        return Convert.ToInt64(_database.Scalar(
            $"SELECT COALESCE(SUM(total_cents), 0) FROM order_items WHERE id IN ({marks})",
            [.. ids.Select((id, i) => ($"$i{i}", (object?)id))]));
    }

    /// <summary>Muda os itens de comanda, com a cozinha, e recalcula as duas contas da soma dos itens vivos.</summary>
    private long Move(SqliteTransaction transaction, IReadOnlyList<string> ids, string sourceId, string targetId)
    {
        var marks = string.Join(",", ids.Select((_, i) => $"$i{i}"));
        (string, object?)[] parameters = [.. ids.Select((id, i) => ($"$i{i}", (object?)id))];
        var moved = Convert.ToInt64(transaction.Command(
            $"SELECT COALESCE(SUM(total_cents), 0) FROM order_items WHERE id IN ({marks})", parameters).ExecuteScalar());
        transaction.Command(
            $"UPDATE order_items SET order_id = $target, is_synced = 0 WHERE id IN ({marks})",
            [("$target", targetId), .. parameters]).ExecuteNonQuery();
        transaction.Command(
            $"UPDATE kds_tickets SET order_id = $target, updated_at = $now WHERE order_item_id IN ({marks})",
            [("$target", targetId), ("$now", Iso.Now(_clock)), .. parameters]).ExecuteNonQuery();
        // Os itens antes das contas: na nuvem, item só muda para comanda que ainda está aberta.
        foreach (var itemId in ids)
        {
            _outbox.Enqueue(transaction, "order_items", itemId, Iso.NewId(), "update",
                new Dictionary<string, object?> { ["id"] = itemId, ["order_id"] = targetId });
        }
        var now = Iso.Now(_clock);
        foreach (var orderId in new[] { sourceId, targetId })
        {
            var subtotal = Convert.ToInt64(transaction.Command(
                "SELECT COALESCE(SUM(total_cents), 0) FROM order_items WHERE order_id = $id AND canceled_at IS NULL",
                ("$id", orderId)).ExecuteScalar());
            transaction.Command(
                "UPDATE orders SET subtotal_cents = $sub, total_cents = $sub - discount_cents, updated_at = $now, " +
                "is_synced = 0 WHERE id = $id",
                ("$sub", subtotal), ("$now", now), ("$id", orderId)).ExecuteNonQuery();
            _outbox.Enqueue(transaction, "orders", orderId, Iso.NewId(), "update", new Dictionary<string, object?>
            {
                ["id"] = orderId, ["subtotal_cents"] = subtotal, ["total_cents"] = subtotal,
            });
        }
        return moved;
    }

    private void PublishMove(TableOrder source, TableOrder target, int count, long total) =>
        _hub.Publish("order.items_moved", new JsonObject
        {
            ["from_order_id"] = source.Id, ["from_table"] = source.TableLabel, ["to_order_id"] = target.Id,
            ["to_table"] = target.TableLabel, ["items"] = count, ["total_cents"] = total,
        });

    private TableOrder? FindByClientUuid(string clientUuid) =>
        _database.Scalar("SELECT id FROM orders WHERE client_uuid = $uuid AND tenant_id = $tenant",
            ("$uuid", clientUuid), ("$tenant", _terminal.TenantId)) is string id ? GetOrder(id) : null;

    private TableOrder? OpenOrderOf(string tableId) =>
        Query(null, Select + "WHERE o.tenant_id = $tenant AND o.table_id = $table AND o.status = 'open' " +
                             "ORDER BY o.opened_at LIMIT 1", ("$tenant", _terminal.TenantId), ("$table", tableId))
            .FirstOrDefault();

    /// <summary>Id ou rótulo, e sempre mesa do cadastro: rótulo livre foi o que criou "mesa 5", "Mesa 5" e "M5".</summary>
    private StoreTable ResolveTable(string? tableId, string label)
    {
        var tables = new TableService(_database, _terminal, _clock);
        if (!string.IsNullOrEmpty(tableId))
        {
            var table = tables.Get(tableId);
            return table.IsActive ? table : throw new TableException($"A {table.Label} está fora do mapa do salão.");
        }
        var cleaned = label.Trim();
        var found = cleaned.Length > 0 ? tables.FindByLabel(label) : null;
        return found ?? throw new TableException(cleaned.Length > 0
            ? $"Mesa {TableService.PyRepr(cleaned)} não existe no cadastro. Cadastre-a nas opções de gerente antes de usá-la."
            : "Escolha uma mesa do salão.");
    }

    private TableOrder RequireOpen(string orderId)
    {
        var order = GetOrder(orderId);
        return order.Status == "open"
            ? order
            : throw new OrderClosedException(
                $"O pedido {order.LocalNumber} está {order.Status}. Um pedido fechado altera-se por estorno, não por edição.");
    }

    private List<TableOrder> Query(SqliteTransaction? transaction, string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(_database.Connection, transaction, sql, parameters);
        using var reader = command.ExecuteReader();
        var orders = new List<TableOrder>();
        while (reader.Read())
        {
            string? Text(int i) => reader.IsDBNull(i) ? null : reader.GetValue(i).ToString();
            orders.Add(new TableOrder(
                reader.GetString(0), reader.GetString(1), reader.GetInt64(2), Text(3) ?? "", reader.GetString(4),
                reader.GetInt64(5), Convert.ToInt32(reader.GetInt64(14)),
                Text(7) is { Length: > 0 } table ? table : null,
                Text(8) is { Length: > 0 } bill ? bill : null,
                Text(11) is { Length: > 0 } op ? op : null,
                Text(13) ?? "",
                reader.IsDBNull(6) ? 0 : reader.GetInt64(6),
                Text(12) is { Length: > 0 } opened ? opened : null,
                reader.IsDBNull(9) ? 0 : reader.GetInt64(9),
                reader.IsDBNull(10) ? 0 : reader.GetInt64(10)));
        }
        return orders;
    }
}
