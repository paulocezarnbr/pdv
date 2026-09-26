using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Pdv.Data;
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
public sealed partial class SalonContractTests : IDisposable
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
        foreach (var (table, rows) in Contract["seed"]!.AsObject())
        {
            foreach (var row in rows!.AsArray().Select(r => r!.AsObject()))
            {
                _database.Execute(
                    $"INSERT INTO {table} ({string.Join(", ", row.Select(c => c.Key))}) " +
                    $"VALUES ({string.Join(", ", row.Select(c => "$" + c.Key))})",
                    [.. row.Select(c => ("$" + c.Key, Scalar(c.Value)))]);
            }
        }
    }

    public void Dispose()
    {
        _database.Dispose();
        _file.Dispose();
    }

    private static object? Scalar(JsonNode? node) => node?.GetValueKind() switch
    {
        null or JsonValueKind.Null => null,
        JsonValueKind.String => node.GetValue<string>(),
        JsonValueKind.True => 1,
        JsonValueKind.False => 0,
        _ => node.GetValue<JsonElement>().GetInt64(),
    };

    [GeneratedRegex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")]
    private static partial Regex Uuid();

    [GeneratedRegex(@"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z|[+-]\d{2}:\d{2})?")]
    private static partial Regex Timestamp();

    /// <summary>O <c>Normalizer</c> de <c>salon_script.py</c>: UUID → &lt;idN&gt; na ordem de aparição, horário → &lt;ts&gt;.</summary>
    private sealed class Normalizer
    {
        private readonly Dictionary<string, string> _ids = [];

        public JsonNode? Value(JsonNode? node) => node switch
        {
            null => null,
            JsonObject obj => new JsonObject(obj.Select(p => KeyValuePair.Create(p.Key, Value(p.Value)))),
            JsonArray array => new JsonArray([.. array.Select(Value)]),
            JsonValue value when value.GetValueKind() == JsonValueKind.String => Text(value.GetValue<string>()),
            _ => node.DeepClone(),
        };

        private string Text(string text)
        {
            text = Timestamp().Replace(text, "<ts>");
            return Uuid().Replace(text, match =>
            {
                if (!_ids.TryGetValue(match.Value, out var alias))
                {
                    alias = $"<id{_ids.Count + 1}>";
                    _ids[match.Value] = alias;
                }
                return alias;
            });
        }
    }

    /// <summary>Um passo do roteiro, como o <c>handlers</c> do Python o executa.</summary>
    private JsonNode? Execute(string op, JsonObject args, TableService tables)
    {
        string Text(string name) => args[name]!.GetValue<string>();
        string? Optional(string name) => args[name]?.GetValue<string>();
        int? Number(string name) => args[name] is { } value ? value.GetValue<int>() : null;
        bool Flag(string name) => args[name]?.GetValue<bool>() ?? false;

        return op switch
        {
            "tables.list" => new JsonArray([.. tables.List(Flag("include_inactive")).Select(t => (JsonNode)t.ToJson())]),
            "tables.seed" => tables.SeedDefaultTables(Number("count")!.Value),
            "tables.create" => tables.Create(Text("label"), Optional("area") ?? "Salão", Number("seats") ?? 4, Number("sort_order")).ToJson(),
            "tables.update" => tables.Update(Text("table_id"), Optional("label"), Optional("area"), Number("seats"), Number("sort_order")).ToJson(),
            "tables.set_active" => tables.SetActive(Text("table_id"), Flag("active")).ToJson(),
            "tables.find" => tables.FindByLabel(Text("label"))?.ToJson(),
            _ => throw new InvalidOperationException($"Passo desconhecido no roteiro: {op}"),
        };
    }

    private (JsonArray Results, JsonArray Outbox) Run()
    {
        var tables = new TableService(_database, _terminal);
        var saved = new Dictionary<string, string>();
        var normalizer = new Normalizer();
        var results = new JsonArray();
        foreach (var step in Contract["script"]!.AsArray().Select(s => s!.AsObject()))
        {
            var args = new JsonObject();
            foreach (var (key, value) in step["args"]?.AsObject() ?? [])
            {
                args[key] = value is JsonValue v && v.GetValueKind() == JsonValueKind.String && v.GetValue<string>().StartsWith('$')
                    ? saved[v.GetValue<string>()[1..]]
                    : value?.DeepClone();
            }
            JsonObject outcome;
            try
            {
                var result = Execute(step["op"]!.GetValue<string>(), args, tables);
                if (step["save"] is { } save) saved[save.GetValue<string>()] = result!["id"]!.GetValue<string>();
                outcome = new JsonObject { ["result"] = result };
            }
            catch (TableException error)
            {
                outcome = new JsonObject { ["error"] = error.Message };
            }
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
