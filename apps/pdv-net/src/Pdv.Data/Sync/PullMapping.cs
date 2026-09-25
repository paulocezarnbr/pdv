using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using Pdv.Core.Audit;

namespace Pdv.Data.Sync;

/// <summary>
/// Como uma linha de cadastro da nuvem vira uma linha no banco do caixa — o
/// <c>pull_mapping</c> do Python, conferido caso a caso por <c>contracts/sync.json</c>.
/// </summary>
/// <remarks>
/// <para>
/// Cada tabela diz o que aceita. Coluna nova na nuvem é ignorada em vez de
/// derrubar o caixa, nome de coluna nunca vem da rede, e o que o caixa exige e
/// a nuvem não manda (a loja do produto) vem da identidade do terminal.
/// </para>
/// <para>
/// Receitas, linhas de receita e insumos ficam de fora até os modelos
/// convergirem: converter rendimento e custo por adivinhação erra a baixa de
/// estoque, e o erro aparece no CMV do mês.
/// </para>
/// </remarks>
public static partial class PullMapping
{
    /// <summary>Cadastros que a nuvem oferece, na ordem em que o caixa pede.</summary>
    public static readonly IReadOnlyList<string> PullableTables = ["products", "recipes", "recipe_lines", "inventory_items", "users"];

    /// <summary>O que a nuvem oferece e o caixa ainda não aplica com segurança — e por quê.</summary>
    public static readonly IReadOnlyDictionary<string, string> NotApplied = new Dictionary<string, string>
    {
        ["recipes"] = "modelo de rendimento diferente (yield_grams × base_qty_g/yield_factor)",
        ["recipe_lines"] = "quantidade por base diferente (quantity_mg × qty_per_base_mg)",
        ["inventory_items"] = "o saldo é do caixa; custo em unidades diferentes",
    };

    private sealed record Mapping(
        IReadOnlyDictionary<string, Func<JsonElement, object?>> Columns,
        IReadOnlyList<string> Required,
        Func<string, IReadOnlyDictionary<string, object>> Fill);

    private static readonly IReadOnlyDictionary<string, Mapping> Mappings = new Dictionary<string, Mapping>
    {
        ["users"] = new(
            new Dictionary<string, Func<JsonElement, object?>>
            {
                ["id"] = Text, ["tenant_id"] = Text, ["name"] = Text, ["login"] = Text, ["role"] = Text,
                ["pin_hash"] = Text, ["max_discount_percent"] = Text, ["can_authorize"] = Flag,
                ["is_active"] = Flag, ["updated_at"] = Text,
            },
            ["id", "tenant_id", "name", "login", "role", "updated_at"],
            _ => new Dictionary<string, object>()),
        ["products"] = new(
            new Dictionary<string, Func<JsonElement, object?>>
            {
                ["id"] = Text, ["tenant_id"] = Text, ["sku"] = Text, ["barcode"] = Text, ["name"] = Text,
                ["category"] = Text, ["pricing_mode"] = Text, ["price_cents"] = Int, ["tare_grams"] = Int,
                ["is_active"] = Flag, ["updated_at"] = Text, ["server_seq"] = Int,
            },
            ["id", "tenant_id", "sku", "name", "pricing_mode", "price_cents", "updated_at"],
            // O catálogo da nuvem vale para a rede; no caixa, o produto é desta loja.
            storeId => new Dictionary<string, object> { ["store_id"] = storeId }),
    };

    public static IEnumerable<string> MappedTables => Mappings.Keys;

    public static bool IsMapped(string table) => Mappings.ContainsKey(table);

    /// <summary>
    /// A linha pronta para o banco do caixa, ou <c>null</c> se ela não serve:
    /// outro tenant, campo obrigatório faltando ou valor que não converte.
    /// </summary>
    /// <remarks>
    /// A nuvem já filtra por tenant; conferir de novo custa uma comparação e é
    /// o que impede um cadastro de outra rede de virar login neste caixa. Nulo
    /// vindo da nuvem é "sem valor", não "apague": fica o padrão do caixa.
    /// </remarks>
    public static IReadOnlyDictionary<string, object>? MapRow(string table, JsonElement row, string tenantId, string storeId)
    {
        var mapping = Mappings[table];
        if (row.ValueKind != JsonValueKind.Object) return null;
        if (!row.TryGetProperty("tenant_id", out var tenant) || PyStr(tenant) != tenantId) return null;
        foreach (var name in mapping.Required)
        {
            if (!row.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null ||
                (value.ValueKind == JsonValueKind.String && value.GetString()!.Length == 0))
            {
                return null;
            }
        }

        var values = new Dictionary<string, object>(StringComparer.Ordinal);
        foreach (var (name, convert) in mapping.Columns)
        {
            if (!row.TryGetProperty(name, out var raw) || raw.ValueKind == JsonValueKind.Null) continue;
            var converted = convert(raw);
            if (converted is null) return null;
            values[name] = converted;
        }
        foreach (var (name, value) in mapping.Fill(storeId)) values[name] = value;
        return values;
    }

    // -- as conversões do Python -------------------------------------------------

    /// <summary><c>str(value)</c> do Python sobre o que o <c>json.loads</c> produz.</summary>
    private static object? Text(JsonElement value) => PyStr(value);

    private static string? PyStr(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString(),
        JsonValueKind.True => "True",
        JsonValueKind.False => "False",
        JsonValueKind.Number => IsIntegerLiteral(value.GetRawText())
            ? System.Numerics.BigInteger.Parse(value.GetRawText(), CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture)
            : PythonFloat.Repr(value.GetDouble()),
        JsonValueKind.Null => "None",
        _ => value.GetRawText(),
    };

    /// <summary>
    /// <c>int(str(value))</c>: o driver da nuvem manda BIGINT como texto
    /// (<c>"700"</c>). Aceita espaço em volta, sinal e <c>_</c> entre dígitos,
    /// como o Python; recusa <c>"12.0"</c>, número com fração e booleano.
    /// </summary>
    private static object? Int(JsonElement value)
    {
        var text = value.ValueKind switch
        {
            JsonValueKind.String => value.GetString()!,
            JsonValueKind.Number when IsIntegerLiteral(value.GetRawText()) => value.GetRawText(),
            _ => null,
        };
        if (text is null) return null;
        var match = PythonIntLiteral().Match(text);
        if (!match.Success) return null;
        return long.TryParse(match.Groups["number"].Value.Replace("_", ""), NumberStyles.AllowLeadingSign,
            CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : null;
    }

    /// <summary>
    /// Texto: <c>1/true/t/yes</c>, sem caixa e sem espaço em volta. Resto: a
    /// "verdade" do Python — zero, vazio e falso são 0.
    /// </summary>
    private static object? Flag(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString()!.Trim().ToLowerInvariant() is "1" or "true" or "t" or "yes" ? 1 : 0,
        JsonValueKind.True => 1,
        JsonValueKind.False => 0,
        JsonValueKind.Number => value.GetDouble() != 0 ? 1 : 0,
        JsonValueKind.Array => value.GetArrayLength() > 0 ? 1 : 0,
        JsonValueKind.Object => value.EnumerateObject().Any() ? 1 : 0,
        _ => 0,
    };

    private static bool IsIntegerLiteral(string raw) => raw.IndexOfAny(['.', 'e', 'E']) < 0;

    [GeneratedRegex(@"^\s*(?<number>[+-]?[0-9]+(?:_[0-9]+)*)\s*$")]
    private static partial Regex PythonIntLiteral();
}
