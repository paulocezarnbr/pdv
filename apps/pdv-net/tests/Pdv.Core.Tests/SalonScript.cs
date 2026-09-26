using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Pdv.Data;

namespace Pdv.Core.Tests;

/// <summary>
/// O que os dois contratos do salão (<c>salon.json</c> e <c>salon-http.json</c>)
/// compartilham com os geradores em Python: a carga do banco e a normalização
/// de ids, horários e valores voláteis.
/// </summary>
internal static partial class SalonScript
{
    /// <summary>As linhas do <c>seed</c> do contrato, na ordem em que o Python as insere.</summary>
    public static void Seed(PdvDatabase database, JsonObject contract)
    {
        foreach (var (table, rows) in contract["seed"]!.AsObject())
        {
            foreach (var row in rows!.AsArray().Select(r => r!.AsObject()))
            {
                database.Execute(
                    $"INSERT INTO {table} ({string.Join(", ", row.Select(c => c.Key))}) " +
                    $"VALUES ({string.Join(", ", row.Select(c => "$" + c.Key))})",
                    [.. row.Select(c => ("$" + c.Key, Scalar(c.Value)))]);
            }
        }
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

    /// <summary>O <c>_VOLATILE</c> do Python: valor que depende do relógio ou da chave, não da regra.</summary>
    private static readonly Dictionary<string, string> Volatile = new(StringComparer.Ordinal)
    {
        ["hash"] = "<hash>", ["prev_hash"] = "<hash>", ["waiting_seconds"] = "<s>",
        ["token"] = "<token>", ["code"] = "<code>", ["remaining_seconds"] = "<s>", ["expires_in_seconds"] = "<s>",
    };

    /// <summary>O <c>Normalizer</c> de <c>salon_script.py</c>: UUID → &lt;idN&gt; na ordem de aparição, horário → &lt;ts&gt;.</summary>
    internal sealed class Normalizer
    {
        private readonly Dictionary<string, string> _ids = [];

        public JsonNode? Value(JsonNode? node) => node switch
        {
            null => null,
            JsonObject obj => new JsonObject(obj.Select(p => KeyValuePair.Create(p.Key,
                Volatile.TryGetValue(p.Key, out var mark) && p.Value is not null ? JsonValue.Create(mark) : Value(p.Value)))),
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

}
