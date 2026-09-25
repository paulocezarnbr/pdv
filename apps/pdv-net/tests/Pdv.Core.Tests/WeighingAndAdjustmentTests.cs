using System.Text.Json;
using Pdv.Core.Scale;
using Pdv.Core.Stock;
using Pdv.Core.Tef;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Hardware;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>Item pesado, cancelamento e desconto sobre o banco real (C3c).</summary>
public sealed class WeighingAndAdjustmentTests : IDisposable
{
    private static readonly byte[] Secret = Enumerable.Range(0, 32).Select(i => (byte)i).ToArray();
    private static readonly TerminalIdentity Terminal = new("tenant-1", "store-1", "device-1");
    private static readonly DateTimeOffset At = new(2026, 9, 25, 12, 0, 0, TimeSpan.Zero);

    private static readonly Identity Manager = new("u-gerente", "Bruno Gerente", "bruno", "manager", true, 30m);
    private static readonly Identity Owner = new("u-dona", "Carla Dona", "carla", "owner", true, 100m);
    private static readonly Identity Cashier = new("u-caixa", "Ana Caixa", "ana", "cashier", false, 0m);

    private static readonly JsonElement PushDay =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("push-day.json"))).RootElement.Clone();

    private readonly TestDatabase _file = new();
    private readonly PdvDatabase _database;
    private readonly AuditLedger _ledger = new(Terminal.TenantId, Terminal.StoreId, Terminal.DeviceId, Secret);

    public WeighingAndAdjustmentTests()
    {
        _database = new PdvDatabase(_file.Path);
        Fixtures.SeedCatalog(_database);
        foreach (var user in new[] { Manager, Owner, Cashier })
        {
            _database.Execute(
                "INSERT INTO users (id, tenant_id, name, login, role, pin_hash, max_discount_percent, can_authorize, is_active, updated_at) " +
                "VALUES ($id, 'tenant-1', $name, $login, $role, NULL, $discount, $auth, 1, 'x')",
                ("$id", user.Id), ("$name", user.Name), ("$login", user.Login), ("$role", user.Role),
                ("$discount", user.MaxDiscountPercent.ToString(System.Globalization.CultureInfo.InvariantCulture)),
                ("$auth", user.CanAuthorize ? 1 : 0));
        }
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private ItemRegistration Items() => new(_database, Terminal, _ledger);

    private SaleAdjustments Adjustments() => new(_database, Terminal, _ledger);

    private Product Product(string id) => new Catalog(_database.Connection, Terminal.TenantId).Get(id)!;

    private long Scalar(string sql) => Convert.ToInt64(_database.Scalar(sql));

    /// <summary>Um quadro da Toledo, pelo protocolo de verdade.</summary>
    private static ScaleReading Weigh(string frame) =>
        new ToledoPrix3Protocol().Parse(System.Text.Encoding.ASCII.GetBytes(frame), At);

    private ItemResult Torta(string? orderId = null, string frame = "00847") =>
        Items().RegisterWeighedItem(orderId, Product("p-torta-kg"), Weigh(frame), "u-caixa");

    // -- item pesado ---------------------------------------------------------

    [Fact]
    public void A_weighed_item_charges_the_net_weight_and_writes_off_the_recipe()
    {
        var result = Torta();

        // R$ 49,90/kg × 847 g = 4226,53 → 4227
        Assert.Equal(4227, result.Item.TotalCents);
        Assert.Equal(4227, result.Order.TotalCents);
        Assert.Equal(847, result.Item.NetWeightGrams);

        var expected = RecipeExplosion.Explode(new Catalog(_database.Connection, "tenant-1").RecipeFor(Product("p-torta-kg"))!, 847);
        Assert.Equal(expected, result.Item.Consumptions);
        Assert.Equal(1_000_000 - expected.Single(c => c.InventoryItemId == "farinha").ConsumedMg,
            Scalar("SELECT balance_mg FROM inventory_items WHERE id = 'farinha'"));
        // Chocolate: 10 g em estoque para ~27,8 g de consumo — avisa e vende.
        Assert.Contains(result.StockWarnings, warning => warning.StartsWith("Chocolate"));

        Assert.Equal("00847", _database.Scalar($"SELECT scale_reading_raw FROM order_items WHERE id = '{result.Item.Id}'"));
        Assert.Equal("weight", _database.Scalar($"SELECT pricing_mode FROM order_items WHERE id = '{result.Item.Id}'"));
        Assert.Equal("1", _database.Scalar($"SELECT quantity FROM order_items WHERE id = '{result.Item.Id}'"));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void The_scale_says_one_thing_and_the_charge_another_two_events()
    {
        var result = Torta();
        var events = AuditPayloads();

        Assert.Equal(["weight_captured", "item_registered"], events.Select(e => e.Type));
        var captured = events[0].Payload;
        Assert.Equal("00847", captured.GetProperty("scale_raw_frame").GetString());
        Assert.Equal("stable", captured.GetProperty("scale_status").GetString());
        Assert.Equal("2026-09-25T12:00:00.000000+00:00", captured.GetProperty("read_at").GetString());
        Assert.Equal(4227, events[1].Payload.GetProperty("total_cents").GetInt64());
        Assert.Equal(result.Item.Id, events[1].Payload.GetProperty("order_item_id").GetString());
    }

    [Fact]
    public void Weighed_items_and_their_audit_go_up_with_the_keys_of_the_push_contract()
    {
        Torta();

        Assert.Equal(Keys(ContractPayload("order_items", "insert", p => p.GetProperty("pricing_mode").GetString() == "weight")),
            Keys(Outbox("order_items", "insert")));
        Assert.Equal(Keys(ContractAudit("weight_captured")), Keys(AuditPayloads()[0].Payload));
        Assert.Equal(Keys(ContractAudit("item_registered", p => p.TryGetProperty("net_grams", out _))),
            Keys(AuditPayloads()[1].Payload));
    }

    [Theory]
    [InlineData("IIIII")]
    [InlineData("SSSSS")]
    [InlineData("NNNNN")]
    [InlineData("00000")]
    public void Only_a_stable_weight_is_sold_and_nothing_is_written_otherwise(string frame)
    {
        Assert.Throws<UnstableWeightException>(() => Torta(frame: frame));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM sync_outbox"));
    }

    [Theory]
    [InlineData(ScaleStatus.Unstable)]
    [InlineData(ScaleStatus.Overload)]
    [InlineData(ScaleStatus.Error)]
    public void A_weight_that_is_not_stable_is_refused_even_when_it_has_grams(ScaleStatus status)
    {
        // A sobrecarga por capacidade (SerialScale) e um driver futuro podem
        // trazer gramas junto de um status ruim: vale o status.
        var reading = new ScaleReading(status, 847, "00847", At);
        var error = Assert.Throws<UnstableWeightException>(() =>
            Items().RegisterWeighedItem(null, Product("p-torta-kg"), reading, "u-caixa"));
        Assert.Contains(reading.StatusName, error.Message);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
    }

    [Fact]
    public void Tare_bigger_than_the_weight_leaves_no_empty_order_behind()
    {
        var error = Assert.Throws<InvalidQuantityException>(() =>
            Items().RegisterWeighedItem(null, Product("p-torta-kg"), Weigh("00100"), "u-caixa", tareOverrideGrams: 150));
        Assert.Contains("Tara (150 g) maior", error.Message);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
    }

    [Fact]
    public void The_tare_is_not_charged()
    {
        var result = Items().RegisterWeighedItem(null, Product("p-torta-kg"), Weigh("01000"), "u-caixa", tareOverrideGrams: 153);
        Assert.Equal(847, result.Item.NetWeightGrams);
        Assert.Equal(4227, result.Item.TotalCents);
        Assert.Equal(1000, Scalar($"SELECT gross_weight_grams FROM order_items WHERE id = '{result.Item.Id}'"));
        Assert.Equal(153, Scalar($"SELECT tare_grams FROM order_items WHERE id = '{result.Item.Id}'"));
    }

    [Fact]
    public void A_weighed_product_without_recipe_is_a_catalog_error()
    {
        var error = Assert.Throws<RecipeNotFoundException>(() =>
            Items().RegisterWeighedItem(null, Product("p-kg"), Weigh("00500"), "u-caixa"));
        Assert.Contains("ficha técnica", error.Message);
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM orders"));
    }

    [Fact]
    public void A_unit_product_does_not_go_on_the_scale()
    {
        Assert.Throws<InvalidQuantityException>(() =>
            Items().RegisterWeighedItem(null, Product("p-fatia"), Weigh("00500"), "u-caixa"));
    }

    [Fact]
    public void The_scale_settings_come_from_what_detection_wrote()
    {
        Assert.True(ScaleSettings.Load(_database).IsSimulated);

        _database.Execute(
            "INSERT INTO device_settings (key, value, updated_at) VALUES " +
            "('scale.protocol', 'urano', 'x'), ('scale.port', 'COM4', 'x'), ('scale.baudrate', '4800', 'x')");
        var settings = ScaleSettings.Load(_database);
        Assert.Equal(("urano", "COM4", 4800), (settings.Protocol, settings.Port, settings.BaudRate));
        Assert.IsType<SerialScale>(settings.BuildDriver());
    }

    // -- cancelamento --------------------------------------------------------

    [Fact]
    public void A_manager_cancels_an_item_and_the_stock_comes_back()
    {
        var first = Items().RegisterUnitItem(null, Product("p-fatia"), 1m, "u-caixa");
        var torta = Torta(first.Order.Id);

        var order = Adjustments().CancelItem(first.Order.Id, torta.Item.Id, "u-caixa", Manager, "cliente desistiu");

        Assert.Equal(1450, order.TotalCents);
        Assert.Equal(1000000 - first.Item.Consumptions.Single(c => c.InventoryItemId == "farinha").ConsumedMg,
            Scalar("SELECT balance_mg FROM inventory_items WHERE id = 'farinha'"));
        Assert.Equal("u-gerente", _database.Scalar($"SELECT canceled_by_user_id FROM order_items WHERE id = '{torta.Item.Id}'"));
        Assert.Equal(2, Scalar("SELECT COUNT(*) FROM stock_movements WHERE movement_type = 'adjustment' AND qty_mg > 0"));

        var canceled = AuditPayloads().Last();
        Assert.Equal("item_canceled", canceled.Type);
        Assert.Equal("critical", _database.Scalar("SELECT severity FROM audit_ledger ORDER BY seq DESC LIMIT 1"));
        Assert.Equal("u-gerente", _database.Scalar("SELECT authorizer_user_id FROM audit_ledger ORDER BY seq DESC LIMIT 1"));
        Assert.Equal("u-caixa", _database.Scalar("SELECT actor_user_id FROM audit_ledger ORDER BY seq DESC LIMIT 1"));
        Assert.Equal(847, canceled.Payload.GetProperty("net_grams").GetInt64());
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void The_cancel_goes_up_with_the_keys_of_the_push_contract()
    {
        var torta = Torta();
        Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "erro de balança");

        Assert.Equal(Keys(ContractPayload("order_items", "update", _ => true)), Keys(Outbox("order_items", "update")));
        Assert.Equal(Keys(ContractPayload("stock_movements", "insert", p => p.GetProperty("movement_type").GetString() == "adjustment")),
            Keys(Outbox("stock_movements", "insert", "adjustment")));
        Assert.Equal(Keys(ContractAudit("item_canceled")), Keys(AuditPayloads().Last().Payload));

        // Mudança, não o item de novo: outro client_uuid, senão a nuvem descarta como reenvio.
        Assert.Equal(2, Scalar($"SELECT COUNT(DISTINCT client_uuid) FROM sync_outbox WHERE entity_table = 'order_items'"));
    }

    [Theory]
    [InlineData("u-dona")]
    [InlineData("u-caixa")]
    public void Only_a_manager_releases_a_cancel_and_a_refusal_leaves_nothing(string authorizerId)
    {
        var torta = Torta();
        var before = Scalar("SELECT COUNT(*) FROM sync_outbox");
        var who = authorizerId == Owner.Id ? Owner : Cashier;

        var error = Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", who, "tentativa"));
        Assert.Contains("gerente", error.Message);
        Assert.Equal(before, Scalar("SELECT COUNT(*) FROM sync_outbox"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM order_items WHERE canceled_at IS NOT NULL"));
    }

    [Fact]
    public void A_manager_deactivated_after_the_pin_releases_nothing()
    {
        var torta = Torta();
        _database.Execute("UPDATE users SET is_active = 0 WHERE id = 'u-gerente'");
        Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "x"));
    }

    [Fact]
    public void A_manager_whose_power_was_revoked_releases_nothing()
    {
        var torta = Torta();
        _database.Execute("UPDATE users SET can_authorize = 0 WHERE id = 'u-gerente'");
        Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "x"));
        Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().ApplyDiscount(torta.Order.Id, 5m, "u-caixa", Manager, "x"));
        Assert.Equal(0, Scalar("SELECT COUNT(*) FROM order_items WHERE canceled_at IS NOT NULL"));
    }

    [Fact]
    public void An_item_is_canceled_once_and_only_in_its_own_sale()
    {
        var torta = Torta();
        var other = Items().RegisterUnitItem(null, Product("p-refri"), 1m, "u-caixa");

        Assert.Throws<InvalidQuantityException>(() =>
            Adjustments().CancelItem(other.Order.Id, torta.Item.Id, "u-caixa", Manager, "venda errada"));
        Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "primeira");
        var again = Assert.Throws<InvalidQuantityException>(() =>
            Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "segunda"));
        Assert.Contains("Item inexistente", again.Message);
        Assert.Equal("primeira", _database.Scalar($"SELECT cancel_reason FROM order_items WHERE id = '{torta.Item.Id}'"));
    }

    [Fact]
    public void A_cancel_needs_a_reason()
    {
        var torta = Torta();
        Assert.Throws<InvalidQuantityException>(() =>
            Adjustments().CancelItem(torta.Order.Id, torta.Item.Id, "u-caixa", Manager, "   "));
    }

    // -- desconto ------------------------------------------------------------

    [Fact]
    public void A_discount_is_stored_in_cents_and_audited_with_who_released_it()
    {
        var torta = Torta();
        var (discount, order) = Adjustments().ApplyDiscount(torta.Order.Id, 10m, "u-caixa", Manager, "cliente fiel");

        Assert.Equal(423, discount); // 422,7
        Assert.Equal(4227 - 423, order.TotalCents);
        Assert.Equal(423, Scalar($"SELECT discount_cents FROM orders WHERE id = '{torta.Order.Id}'"));

        var applied = AuditPayloads().Last();
        Assert.Equal("discount_applied", applied.Type);
        Assert.Equal("10", applied.Payload.GetProperty("percent").GetString());
        Assert.Equal("warning", _database.Scalar("SELECT severity FROM audit_ledger ORDER BY seq DESC LIMIT 1"));
        Assert.Equal("u-gerente", _database.Scalar("SELECT authorizer_user_id FROM audit_ledger ORDER BY seq DESC LIMIT 1"));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void The_ceiling_is_the_authorizers_profile_checked_again_in_the_transaction()
    {
        var torta = Torta();
        var error = Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().ApplyDiscount(torta.Order.Id, 31m, "u-caixa", Manager, "amigo"));
        Assert.Equal("Bruno Gerente pode conceder até 30% — o pedido é de 31%.", error.Message);
        Assert.Equal(0, Scalar($"SELECT discount_cents FROM orders WHERE id = '{torta.Order.Id}'"));

        // O teto baixou depois do PIN: vale o de agora.
        _database.Execute("UPDATE users SET max_discount_percent = '5' WHERE id = 'u-gerente'");
        Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().ApplyDiscount(torta.Order.Id, 10m, "u-caixa", Manager, "amigo"));
        Assert.Throws<AuthorizationRequiredException>(() =>
            Adjustments().ApplyDiscount(torta.Order.Id, 1m, "u-caixa", Cashier, "eu mesma"));
    }

    [Fact]
    public void A_new_discount_replaces_the_old_one()
    {
        var torta = Torta();
        Adjustments().ApplyDiscount(torta.Order.Id, 10m, "u-caixa", Manager, "a");
        var (discount, order) = Adjustments().ApplyDiscount(torta.Order.Id, 5m, "u-caixa", Manager, "b");
        Assert.Equal(211, discount); // 211,35
        Assert.Equal(4227 - 211, order.TotalCents);
    }

    [Fact]
    public void Canceling_after_a_discount_never_leaves_the_discount_above_the_subtotal()
    {
        var first = Items().RegisterUnitItem(null, Product("p-refri"), 1m, "u-caixa");
        var torta = Torta(first.Order.Id);
        Adjustments().ApplyDiscount(first.Order.Id, 100m, "u-caixa", Owner, "cortesia");

        var order = Adjustments().CancelItem(first.Order.Id, torta.Item.Id, "u-caixa", Manager, "trocou");

        Assert.Equal((333L, 333L, 0L), (order.SubtotalCents, order.DiscountCents, order.TotalCents));
    }

    [Fact]
    public async Task The_discounted_total_is_what_the_card_charges()
    {
        var torta = Torta();
        var (_, order) = Adjustments().ApplyDiscount(torta.Order.Id, 10m, "u-caixa", Manager, "fiel");

        using var journal = new SqliteTefJournal(_file.Path);
        var closed = await new Checkout(_database, Terminal, _ledger, new TefCoordinator(new TefSimulator(), journal))
            .CloseAsync(order.Id, "u-caixa", [PaymentIntent.Card(TefCardType.Debit, order.TotalCents)], new Quiet());

        Assert.IsType<CheckoutResult.Closed>(closed);
        var payload = Outbox("orders", "insert");
        Assert.Equal(423, payload.GetProperty("discount_cents").GetInt64());
        Assert.Equal(3804, payload.GetProperty("total_cents").GetInt64());
        Assert.Equal(3804, Scalar("SELECT amount_cents FROM payments"));
        _ledger.Verify(_database.Connection);
    }

    [Fact]
    public void The_screen_lists_only_live_items()
    {
        var first = Items().RegisterUnitItem(null, Product("p-refri"), 2m, "u-caixa");
        var torta = Torta(first.Order.Id);
        Adjustments().CancelItem(first.Order.Id, first.Item.Id, "u-caixa", Manager, "x");

        var live = Adjustments().LiveItems(first.Order.Id);
        Assert.Equal([torta.Item.Id], live.Select(item => item.Id));
        Assert.True(live[0].IsWeighed);
        Assert.Equal(847, live[0].NetWeightGrams);
    }

    // -- apoio ---------------------------------------------------------------

    private List<(string Type, JsonElement Payload)> AuditPayloads()
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = "SELECT event_type, payload_json FROM audit_ledger ORDER BY seq";
        using var reader = command.ExecuteReader();
        var events = new List<(string, JsonElement)>();
        while (reader.Read())
        {
            events.Add((reader.GetString(0), JsonDocument.Parse(reader.GetString(1)).RootElement.Clone()));
        }
        return events;
    }

    private JsonElement Outbox(string table, string operation, string? movementType = null)
    {
        using var command = _database.Connection.CreateCommand();
        command.CommandText = "SELECT payload_json FROM sync_outbox WHERE entity_table = $t AND operation = $o ORDER BY seq";
        command.Parameters.AddWithValue("$t", table);
        command.Parameters.AddWithValue("$o", operation);
        using var reader = command.ExecuteReader();
        while (reader.Read())
        {
            var payload = JsonDocument.Parse(reader.GetString(0)).RootElement.Clone();
            if (movementType is null || payload.GetProperty("movement_type").GetString() == movementType) return payload;
        }
        throw new InvalidOperationException($"nada no outbox para {table}/{operation}");
    }

    private static JsonElement ContractPayload(string table, string operation, Func<JsonElement, bool> where) =>
        PushDay.GetProperty("items").EnumerateArray()
            .Select(item => (item, payload: item.GetProperty("payload")))
            .First(pair => pair.item.GetProperty("entity_table").GetString() == table &&
                           pair.item.GetProperty("operation").GetString() == operation && where(pair.payload))
            .payload;

    private static JsonElement ContractAudit(string eventType, Func<JsonElement, bool>? where = null) =>
        PushDay.GetProperty("items").EnumerateArray()
            .Where(item => item.GetProperty("entity_table").GetString() == "audit_ledger")
            .Select(item => item.GetProperty("payload"))
            .Where(payload => payload.GetProperty("event_type").GetString() == eventType)
            .Select(payload => JsonDocument.Parse(payload.GetProperty("payload_json").GetString()!).RootElement.Clone())
            .First(payload => where?.Invoke(payload) ?? true);

    private static List<string> Keys(JsonElement element) =>
        element.EnumerateObject().Select(property => property.Name).Order().ToList();

    private sealed class Quiet : ITefInteraction
    {
        public void Show(string message) { }

        public Task<int?> ChooseAsync(string title, IReadOnlyList<string> options, CancellationToken cancellationToken) =>
            Task.FromResult<int?>(0);

        public Task<string?> AskAsync(string prompt, CancellationToken cancellationToken) => Task.FromResult<string?>("");
    }
}
