using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Pdv.Data;
using Pdv.Data.Auth;
using Pdv.Data.Edge;
using Pdv.Data.Sales;

namespace Pdv.Core.Tests;

/// <summary>
/// O servidor do salão (C6e) contra <c>contracts/salon.json</c>: o mesmo
/// roteiro que o PDV em Python rodou, passo a passo, com a mesma resposta.
/// </summary>
/// <remarks>
/// O app do garçom é reaproveitado sem mudar uma linha — então toda resposta,
/// inclusive a de recusa, precisa ser a do Python. Ids e horários saem
/// normalizados como em <c>salon_script.py</c>.
/// </remarks>
public sealed class SalonContractTests : IDisposable
{
    private static readonly JsonObject Contract =
        JsonNode.Parse(File.ReadAllText(TestDatabase.Contract("salon.json")))!.AsObject();

    private readonly TestDatabase _file = new(userVersion: PdvDatabase.SupportedSchemaVersion);
    private readonly PdvDatabase _database;
    private readonly TerminalIdentity _terminal;

    public SalonContractTests()
    {
        _database = new PdvDatabase(_file.Path);
        _terminal = new TerminalIdentity(
            Contract["tenant_id"]!.GetValue<string>(), Contract["store_id"]!.GetValue<string>(),
            Contract["device_id"]!.GetValue<string>());
        SalonScript.Seed(_database, Contract);
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private const string Manager = "5a1a0000-0000-4000-8000-0000000000a2";
    private const string ManagerName = "Bruno Gerente";

    /// <summary>Os serviços do salão sobre o mesmo banco e o mesmo barramento, como o servidor os monta.</summary>
    private sealed record Salon(
        TableService Tables, TableOrderService Orders, KdsService Kds, EdgeAuth Auth, StaffSessions Staff,
        ManagerSessions Managers, StaffReport Report);

    private static JsonObject Device(PairedDevice device) => new()
    {
        ["id"] = device.Id, ["name"] = device.Name, ["kind"] = device.Kind, ["operator_id"] = device.OperatorId,
    };

    private static List<PaymentIntent> Intents(JsonNode? node) =>
        [.. node!.AsArray().Select(p => new PaymentIntent(p!["method"]!.GetValue<string>(), p["amount_cents"]!.GetValue<long>()))];

    /// <summary>O <c>_settled</c> do roteiro: o recebimento como volta à tela do caixa.</summary>
    private static JsonObject Settled(SettledOrder settled) => new()
    {
        ["order"] = settled.Order.ToJson(),
        ["payments"] = new JsonArray([.. settled.Payments.Select(p => (JsonNode)new JsonObject
        {
            ["method"] = p.Method, ["amount_cents"] = p.AmountCents, ["change_cents"] = p.ChangeCents,
        })]),
        ["tip_cents"] = settled.TipCents,
        ["charged_cents"] = settled.ChargedCents,
        ["change_cents"] = settled.ChangeCents,
    };

    /// <summary>Um passo do roteiro, como o <c>handlers</c> do Python o executa.</summary>
    private JsonNode? Execute(string op, JsonObject args, Salon salon)
    {
        string Text(string name) => args[name]!.GetValue<string>();
        string? Optional(string name) => args[name]?.GetValue<string>();
        int? Number(string name) => args[name] is { } value ? value.GetValue<int>() : null;
        bool Flag(string name) => args[name]?.GetValue<bool>() ?? false;
        List<string> Ids(string name) => [.. args[name]!.AsArray().Select(id => id!.GetValue<string>())];
        var (tables, orders, kds, auth, staff, managers, report) = salon;

        return op switch
        {
            "tables.list" => new JsonArray([.. tables.List(Flag("include_inactive")).Select(t => (JsonNode)t.ToJson())]),
            "tables.seed" => tables.SeedDefaultTables(Number("count")!.Value),
            "tables.create" => tables.Create(Text("label"), Optional("area") ?? "Salão", Number("seats") ?? 4, Number("sort_order")).ToJson(),
            "tables.update" => tables.Update(Text("table_id"), Optional("label"), Optional("area"), Number("seats"), Number("sort_order")).ToJson(),
            "tables.set_active" => tables.SetActive(Text("table_id"), Flag("active")).ToJson(),
            "tables.find" => tables.FindByLabel(Text("label"))?.ToJson(),
            "orders.open" => orders.OpenOrder(Text("client_uuid"), Text("operator_id"), _terminal.DeviceId,
                Optional("table_id"), Optional("table_label") ?? "").ToJson(),
            "orders.add_item" => orders.AddItem(Text("order_id"), Text("client_uuid"), Text("product_id"),
                decimal.Parse(Text("quantity"), CultureInfo.InvariantCulture), Optional("notes") ?? "",
                Optional("station") ?? "cozinha", Optional("created_by_user_id")).ToJson(),
            "orders.items" => orders.ListItems(Text("order_id")),
            "orders.get" => orders.GetOrder(Text("order_id")).ToJson(),
            "orders.list_open" => new JsonArray([.. orders.ListOpenOrders().Select(o => (JsonNode)o.ToJson())]),
            "orders.request_bill" => orders.RequestBill(Text("order_id")).ToJson(),
            "orders.clear_bill" => orders.ClearBillRequest(Text("order_id")).ToJson(),
            "orders.transfer" => orders.Transfer(Text("order_id"), Text("table_id"), Manager, ManagerName).ToJson(),
            "orders.move_items" => MoveItems(),
            "orders.merge" => orders.MergeOrders(Text("source_order_id"), Text("target_order_id"), Manager, ManagerName).ToJson(),
            "orders.settle" => Settled(orders.Settle(Text("order_id"), Intents(args["payments"]), Manager, ManagerName,
                Number("tip_cents") ?? 0)),
            "orders.settle_items" => Settled(orders.SettleItems(Text("order_id"), Ids("item_ids"), Intents(args["payments"]),
                Manager, ManagerName, Number("tip_cents") ?? 0)),
            "orders.cancel" => orders.CancelOrder(Text("order_id"), Manager, ManagerName, Text("reason")).ToJson(),
            "kds.list" => new JsonArray([.. kds.ListActive(Optional("station")).Select(t => (JsonNode)t.ToJson())]),
            "kds.get" => kds.Get(Text("ticket_id")).ToJson(),
            "kds.bump" => kds.Bump(Text("ticket_id")).ToJson(),
            "kds.recall" => kds.Recall(Text("ticket_id")).ToJson(),
            "kds.advance" => kds.Advance(Text("ticket_id"), Text("to_status")).ToJson(),
            "auth.create_code" => CreateCode(),
            "auth.active_code" => auth.ActivePairingCode() is { } active
                ? new JsonObject { ["remaining_seconds"] = active.RemainingSeconds }
                : null,
            "auth.revoke_codes" => auth.RevokePairingCodes(),
            "auth.pair" => Pair(),
            "auth.authenticate" => Device(auth.Authenticate(Text("token"))),
            "auth.lock_seconds" => auth.PairingLockSeconds(),
            "auth.revoke" => auth.Revoke(Text("device_id")),
            "auth.devices" => new JsonArray([.. auth.ListDevices().Select(d => (JsonNode)new JsonObject
            {
                ["id"] = d.Id, ["name"] = d.Name, ["kind"] = d.Kind, ["paired_at"] = d.PairedAt,
                ["last_seen_at"] = d.LastSeenAt, ["revoked_at"] = d.RevokedAt,
            })]),
            "staff.login" => staff.Login(Text("login"), Text("pin"), Text("device_id")).ToJson(staff.Clock, withToken: true),
            "staff.require" => staff.Require(Text("token"), Text("device_id")).ToJson(staff.Clock),
            "staff.logout" => staff.Logout(Text("token")),
            "staff.revoke_user" => staff.RevokeUser(Text("user_id")),
            "staff.active" => new JsonArray([.. staff.ListActive().Select(r => (JsonNode)new JsonObject
            {
                ["user_id"] = r.UserId, ["user_name"] = r.UserName, ["role"] = r.Role, ["device_id"] = r.DeviceId,
                ["created_at"] = r.CreatedAt, ["expires_at"] = r.ExpiresAt, ["last_seen_at"] = r.LastSeenAt,
            })]),
            "manager.authorize" => managers.Authorize(Text("login"), Text("pin"), Text("device_id")).ToJson(managers.Clock),
            "manager.require" => ManagerRequire(),
            "manager.revoke" => managers.Revoke(Text("token")),
            "manager.active" => managers.ActiveCount(),
            "report.by_waiter" => new JsonArray([.. report.ByWaiter().Select(r => (JsonNode)r.ToJson())]),
            "report.for_user" => report.ForUser(Text("user_id")),
            "report.totals" => report.Totals(),
            "stock.balances" => Balances(),
            _ => throw new InvalidOperationException($"Passo desconhecido no roteiro: {op}"),
        };

        JsonNode Balances()
        {
            using var command = _database.Connection.CreateCommand();
            command.CommandText = "SELECT name, balance_mg FROM inventory_items ORDER BY name";
            using var reader = command.ExecuteReader();
            var balances = new JsonArray();
            while (reader.Read())
            {
                balances.Add(new JsonObject { ["name"] = reader.GetString(0), ["balance_mg"] = reader.GetInt64(1) });
            }
            return balances;
        }

        JsonNode CreateCode()
        {
            var (code, pairing) = auth.CreatePairingCode();
            return new JsonObject { ["code"] = code, ["remaining_seconds"] = pairing.RemainingSeconds };
        }

        JsonNode Pair()
        {
            var token = auth.Pair(Text("code"), Text("device_name"), Optional("kind") ?? "waiter");
            return new JsonObject { ["token"] = token, ["device"] = Device(auth.Authenticate(token)) };
        }

        JsonNode ManagerRequire()
        {
            var who = managers.Require(Text("token"), Text("device_id"));
            return new JsonObject { ["user_id"] = who.Id, ["name"] = who.Name, ["role"] = who.Role };
        }

        JsonNode MoveItems()
        {
            var (source, target) = orders.MoveItems(Text("source_order_id"), Text("target_order_id"), Ids("item_ids"),
                Manager, ManagerName);
            return new JsonArray(source.ToJson(), target.ToJson());
        }
    }

    /// <summary>As recusas que o roteiro espera: as mesmas classes que no Python herdam de <c>PdvError</c>.</summary>
    private static bool IsRefusal(Exception error) => error is TableException or OrderNotFoundException
        or OrderClosedException or ProductNotSellableException or TableOccupiedException or TicketNotFoundException
        or InvalidTransitionException or InsufficientPaymentException or PairingException or DeviceAuthException
        or StaffAuthException or AuthenticationException;

    /// <summary>O <c>_dig</c> do roteiro: <c>"0.id"</c> → <c>value[0]["id"]</c>.</summary>
    private static string Dig(JsonNode? node, string path)
    {
        foreach (var part in path.Split('.'))
        {
            node = int.TryParse(part, CultureInfo.InvariantCulture, out var index) ? node![index] : node![part];
        }
        return node!.GetValue<string>();
    }

    private static JsonNode? Resolve(JsonNode? node, Dictionary<string, string> saved) => node switch
    {
        // `$nome` também com espaço em volta (" $code1 "): o espaço é parte do que se testa.
        JsonValue v when v.GetValueKind() == JsonValueKind.String && v.GetValue<string>().Trim().StartsWith('$') =>
            v.GetValue<string>().Replace(v.GetValue<string>().Trim(), saved[v.GetValue<string>().Trim()[1..]]),
        JsonArray array => new JsonArray([.. array.Select(item => Resolve(item, saved))]),
        JsonObject obj => new JsonObject(obj.Select(p => KeyValuePair.Create(p.Key, Resolve(p.Value, saved)))),
        _ => node?.DeepClone(),
    };

    private (JsonArray Results, JsonArray Outbox) Run()
    {
        var hub = new EventHub();
        using var events = hub.Subscribe();
        var ledger = new AuditLedger(_terminal.TenantId, _terminal.StoreId, _terminal.DeviceId,
            Encoding.UTF8.GetBytes(Contract["device_secret"]!.GetValue<string>()));
        var salon = new Salon(
            new TableService(_database, _terminal),
            new TableOrderService(_database, _terminal, ledger, hub),
            new KdsService(_database, _terminal, hub),
            new EdgeAuth(_database, _terminal),
            new StaffSessions(_database, _terminal.TenantId),
            new ManagerSessions(new StaffAuthentication(_database, _terminal.TenantId)),
            new StaffReport(_database, _terminal.TenantId));
        var saved = new Dictionary<string, string>();
        var normalizer = new SalonScript.Normalizer();
        var results = new JsonArray();
        foreach (var step in Contract["script"]!.AsArray().Select(s => s!.AsObject()))
        {
            var args = (JsonObject)Resolve(step["args"] ?? new JsonObject(), saved)!;
            JsonObject outcome;
            try
            {
                var result = Execute(step["op"]!.GetValue<string>(), args, salon);
                var paths = step["save"] switch
                {
                    null => [],
                    JsonValue name => [KeyValuePair.Create(name.GetValue<string>(), "id")],
                    JsonObject many => many.Select(p => KeyValuePair.Create(p.Key, p.Value!.GetValue<string>())).ToList(),
                    _ => throw new InvalidOperationException("save inválido no roteiro"),
                };
                foreach (var (name, path) in paths) saved[name] = Dig(result, path);
                outcome = new JsonObject { ["result"] = result };
            }
            catch (Exception error) when (IsRefusal(error))
            {
                outcome = new JsonObject { ["error"] = error.Message };
            }
            // O que o passo publicou, sem o `at`: é o que o KDS e o app do garçom recebem.
            var published = new JsonArray();
            while (events.TryNext(out var edgeEvent))
            {
                var json = edgeEvent!.ToJson();
                json.Remove("at");
                published.Add(json);
            }
            if (published.Count > 0) outcome["events"] = published;
            results.Add(normalizer.Value(outcome));
        }

        var outbox = new JsonArray();
        using var command = _database.Connection.CreateCommand();
        command.CommandText = "SELECT entity_table, operation, payload_json FROM sync_outbox ORDER BY seq";
        using var reader = command.ExecuteReader();
        while (reader.Read())
        {
            outbox.Add(normalizer.Value(new JsonObject
            {
                ["entity_table"] = reader.GetString(0),
                ["operation"] = reader.GetString(1),
                ["payload"] = JsonNode.Parse(reader.GetString(2)),
            }));
        }
        return (results, outbox);
    }

    [Fact]
    public void Every_step_answers_what_the_python_pdv_answered()
    {
        var (results, _) = Run();
        var script = Contract["script"]!.AsArray();
        var expected = Contract["results"]!.AsArray();

        Assert.Equal(expected.Count, results.Count);
        for (var i = 0; i < expected.Count; i++)
        {
            var step = script[i]!.ToJsonString();
            Assert.Equal((step, expected[i]!.ToJsonString()), (step, results[i]!.ToJsonString()));
        }
    }

    [Fact]
    public void The_outbox_is_what_the_python_pdv_queued()
    {
        var (_, outbox) = Run();

        Assert.Equal(Contract["outbox"]!.ToJsonString(), outbox.ToJsonString());
    }
}
