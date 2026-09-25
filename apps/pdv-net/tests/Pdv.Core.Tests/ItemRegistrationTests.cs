using System.Text.Json;
using Pdv.Core.Stock;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

public sealed class ItemRegistrationTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly AuditLedger _ledger = new(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);

    public ItemRegistrationTests()
    {
        _database = new PdvDatabase(_file.Path);
        // Produto e ficha apontam um para o outro: a checagem das chaves fica
        // para o fim da transação, como numa carga de catálogo real.
        _database.Execute(
            """
            BEGIN;
            PRAGMA defer_foreign_keys = ON;
            INSERT INTO inventory_items (id, tenant_id, store_id, name, unit, balance_mg, min_stock_mg, avg_cost_cents_per_kg, updated_at)
            VALUES ('farinha', 'tenant-1', 'store-1', 'Farinha', 'mg', 1000000, 0, 520, 'x'),
                   ('chocolate', 'tenant-1', 'store-1', 'Chocolate', 'mg', 10000, 0, 4890, 'x');
            INSERT INTO recipes (id, tenant_id, product_id, base_qty_g, yield_factor, updated_at)
            VALUES ('r-fatia', 'tenant-1', 'p-fatia', 120, '0.85', 'x');
            INSERT INTO recipe_lines (id, recipe_id, inventory_item_id, qty_per_base_mg, waste_percent, updated_at)
            VALUES ('l1', 'r-fatia', 'farinha', 30000, '0', 'x'), ('l2', 'r-fatia', 'chocolate', 40000, '2.5', 'x');
            INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, category, pricing_mode, price_cents, tare_grams, recipe_id, is_active, updated_at)
            VALUES ('p-fatia', 'tenant-1', 'store-1', 'F1', '7890000000011', 'Fatia de torta', 'Doces', 'unit', 1450, 0, 'r-fatia', 1, 'x'),
                   ('p-refri', 'tenant-1', 'store-1', 'R1', '7890000000028', 'Refrigerante lata', 'Bebidas', 'unit', 333, 0, NULL, 1, 'x'),
                   ('p-kg', 'tenant-1', 'store-1', 'K1', NULL, 'Bolo por quilo', 'Doces', 'weight', 8990, 0, NULL, 1, 'x');
            COMMIT;
            """);
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private ItemRegistration Items(bool block = false) => new(_database, Terminal, _ledger, block);

    private Catalog Catalog() => new(_database.Connection, Terminal.TenantId);

    private long Scalar(string sql) => Convert.ToInt64(_database.Scalar(sql));

    [Fact]
    public void The_first_item_opens_the_sale_and_writes_off_the_recipe()
    {
        var slice = Catalog().FindByBarcode("7890000000011")!;
        var result = Items().RegisterUnitItem(null, slice, 2m, "user-1");

        // 2 fatias = 240 g da ficha de 120 g que rende 85%.
        var expected = RecipeExplosion.Explode(Catalog().RecipeFor(slice)!, 240);
        Assert.Equal(expected, result.Item.Consumptions);
        Assert.Equal(2900, result.Order.TotalCents);
        Assert.Equal(2900, Scalar($"SELECT total_cents FROM orders WHERE id = '{result.Order.Id}'"));

        var farinha = expected.Single(c => c.InventoryItemId == "farinha").ConsumedMg;
        Assert.Equal(1_000_000 - farinha, Scalar("SELECT balance_mg FROM inventory_items WHERE id = 'farinha'"));
        Assert.Equal(2, Scalar("SELECT COUNT(*) FROM stock_movements WHERE qty_mg < 0"));
        Assert.Equal(2, Scalar("SELECT COUNT(*) FROM order_item_ingredients"));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void A_second_item_joins_the_same_sale()
    {
        var first = Items().RegisterUnitItem(null, Catalog().Get("p-fatia")!, 1m, "user-1");
        var second = Items().RegisterUnitItem(first.Order.Id, Catalog().Get("p-refri")!, 1.5m, "user-1");

        Assert.Equal(first.Order.Id, second.Order.Id);
        // 1,5 × R$ 3,33 = 499,5 → 500 (meio para o par, como o Python)
        Assert.Equal(500, second.Item.TotalCents);
        Assert.Equal(1450 + 500, second.Order.TotalCents);
        Assert.Equal("1.5", _database.Scalar($"SELECT quantity FROM order_items WHERE id = '{second.Item.Id}'"));
    }

    [Fact]
    public void Items_and_stock_go_up_with_the_keys_of_the_push_contract()
    {
        Items().RegisterUnitItem(null, Catalog().Get("p-fatia")!, 1m, "user-1");

        var contract = JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("push-day.json"))).RootElement;
        JsonElement Contract(string table) => contract.GetProperty("items").EnumerateArray()
            .First(item => item.GetProperty("entity_table").GetString() == table &&
                           item.GetProperty("operation").GetString() == "insert" &&
                           (table != "order_items" || item.GetProperty("payload").GetProperty("ingredients").GetArrayLength() > 0))
            .GetProperty("payload");
        JsonElement Mine(string table) => JsonDocument.Parse(
            (string)_database.Scalar($"SELECT payload_json FROM sync_outbox WHERE entity_table = '{table}' LIMIT 1")!).RootElement;

        Assert.Equal(Keys(Contract("order_items")), Keys(Mine("order_items")));
        Assert.Equal(Keys(Contract("order_items").GetProperty("ingredients")[0]), Keys(Mine("order_items").GetProperty("ingredients")[0]));
        Assert.Equal(Keys(Contract("stock_movements")), Keys(Mine("stock_movements")));
    }

    private static List<string> Keys(JsonElement element) =>
        element.EnumerateObject().Select(property => property.Name).Order().ToList();

    [Fact]
    public void A_product_without_recipe_sells_without_touching_stock()
    {
        Items().RegisterUnitItem(null, Catalog().Get("p-refri")!, 1m, "user-1");
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM stock_movements"));
    }

    [Fact]
    public void A_weighed_product_is_sent_to_the_scale()
    {
        var error = Assert.Throws<InvalidQuantityException>(() =>
            Items().RegisterUnitItem(null, Catalog().Get("p-kg")!, 1m, "user-1"));
        Assert.Contains("balança", error.Message);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
    }

    [Fact]
    public void Negative_stock_warns_but_sells_by_default()
    {
        // Chocolate: 10 g em estoque; 3 fatias consomem bem mais.
        var result = Items().RegisterUnitItem(null, Catalog().Get("p-fatia")!, 3m, "user-1");
        Assert.Contains(result.StockWarnings, warning => warning.StartsWith("Chocolate"));
        Assert.Equal(1, Scalar("SELECT COUNT(*) FROM order_items"));
    }

    [Fact]
    public void A_store_that_blocks_negative_stock_loses_nothing_half_written()
    {
        Assert.Throws<InsufficientStockException>(() =>
            Items(block: true).RegisterUnitItem(null, Catalog().Get("p-fatia")!, 3m, "user-1"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM order_items"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM sync_outbox"));
    }

    [Fact]
    public void The_search_finds_by_name_and_by_barcode()
    {
        Assert.Equal(["Fatia de torta"], Catalog().Search("torta").Select(p => p.Name));
        Assert.Equal(["Refrigerante lata"], Catalog().Search("7890000000028").Select(p => p.Name));
        Assert.Equal(3, Catalog().Search("").Count);
    }

    [Fact]
    public async Task Item_then_card_is_a_whole_sale()
    {
        var result = Items().RegisterUnitItem(null, Catalog().Get("p-fatia")!, 1m, "user-1");
        using var journal = new SqliteTefJournal(_file.Path);
        var tef = new TefSimulator();
        var closed = await new Checkout(_database, Terminal, _ledger, new TefCoordinator(tef, journal))
            .CloseAsync(result.Order.Id, "user-1", [PaymentIntent.Card(TefCardType.Debit, 1450)], new Quiet());

        Assert.IsType<CheckoutResult.Closed>(closed);
        Assert.Equal("paid", _database.Scalar($"SELECT status FROM orders WHERE id = '{result.Order.Id}'"));
        _ledger.Verify(_database.Connection);
        // pedido criado → item → estoque (2) → auditoria do item → fechamento → pagamento → auditoria da venda
        Assert.Equal(
            ["order_items", "stock_movements", "stock_movements", "audit_ledger", "orders", "payments", "audit_ledger"],
            ReadOutboxTables());
    }

    private List<string> ReadOutboxTables()
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = "SELECT entity_table FROM sync_outbox ORDER BY seq";
        using var reader = command.ExecuteReader();
        var tables = new List<string>();
        while (reader.Read()) tables.Add(reader.GetString(0));
        return tables;
    }

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
