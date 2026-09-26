using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Pdv.Edge;

/// <summary>Uma resposta de erro já decidida: status, <c>detail</c> e, no 403, qual credencial pedir de novo.</summary>
internal sealed class HttpError(int status, JsonNode? detail, string? scope = null) : Exception(detail?.ToJsonString())
{
    public int Status { get; } = status;

    public JsonNode? Detail { get; } = detail;

    /// <summary>O <c>X-Auth-Scope</c>: duas credenciais recusam com o mesmo 403, e o app precisa saber qual pedir.</summary>
    public string? Scope { get; } = scope;

    public static HttpError Validation(IEnumerable<string> problems) =>
        new(422, new JsonArray([.. problems.Select(p => (JsonNode)new JsonObject { ["msg"] = p })]));
}

/// <summary>
/// O corpo da requisição conferido campo a campo, como os modelos pydantic do
/// <c>edge/server.py</c>: tamanho de texto, faixa de número, padrão e padrão
/// de preenchimento. Campo a mais é ignorado, como lá.
/// </summary>
/// <remarks>
/// O 422 sai como uma lista em <c>detail</c>, igual ao FastAPI: o app do garçom
/// mostra "Erro 422" nos dois servidores, e não um texto que só um deles tem.
/// Os problemas são juntados e recusados de uma vez, em <see cref="Done"/>.
/// </remarks>
internal sealed class RequestBody(JsonNode? root)
{
    private readonly JsonObject? _object = root as JsonObject;
    private readonly List<string> _problems = root is null or JsonObject ? [] : ["O corpo precisa ser um objeto JSON."];

    public static RequestBody Missing { get; } = new(null);

    /// <summary>Texto obrigatório, ou com padrão quando <paramref name="fallback"/> é dado.</summary>
    public string Text(string name, int min, int max, string? fallback = null, string? pattern = null)
    {
        var node = Field(name);
        if (node is null)
        {
            if (fallback is null) _problems.Add($"{name}: campo obrigatório.");
            return fallback ?? "";
        }
        return CheckText(name, node, min, max, pattern) ?? "";
    }

    /// <summary>Texto opcional: ausente ou <c>null</c> é "não mexer".</summary>
    public string? OptionalText(string name, int min, int max) =>
        Field(name) is { } node ? CheckText(name, node, min, max, null) : null;

    public int Number(string name, int min, int max, int fallback) => OptionalNumber(name, min, max) ?? fallback;

    public int? OptionalNumber(string name, int min, int max)
    {
        if (Field(name) is not { } node) return null;
        var value = Integer(node);
        if (value is null)
        {
            _problems.Add($"{name}: precisa ser um número inteiro.");
            return null;
        }
        if (value < min || value > max)
        {
            _problems.Add($"{name}: precisa estar entre {min} e {max}.");
            return null;
        }
        return value;
    }

    private static int? Integer(JsonNode node)
    {
        if (node is not JsonValue v) return null;
        switch (v.GetValueKind())
        {
            case JsonValueKind.Number:
                if (v.TryGetValue<int>(out var whole)) return whole;
                // 4.0 vale 4, como no pydantic; 4.5 não.
                return v.TryGetValue<double>(out var real) && real == Math.Floor(real) && Math.Abs(real) < int.MaxValue
                    ? (int)real
                    : null;
            case JsonValueKind.String:
                // O modo "lax" do pydantic aceita número em texto: "4" vale 4.
                return int.TryParse(v.GetValue<string>().Trim(), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture,
                    out var parsed) ? parsed : null;
            default:
                return null;
        }
    }

    public bool? OptionalFlag(string name)
    {
        if (Field(name) is not { } node) return null;
        var value = node is JsonValue v ? v.GetValueKind() switch
        {
            JsonValueKind.True => (bool?)true,
            JsonValueKind.False => false,
            JsonValueKind.String => Flag(v.GetValue<string>()),
            JsonValueKind.Number => v.TryGetValue<int>(out var n) && n is 0 or 1 ? n == 1 : null,
            _ => null,
        } : null;
        if (value is null) _problems.Add($"{name}: precisa ser verdadeiro ou falso.");
        return value;
    }

    /// <summary>Os textos que o pydantic aceita como booleano, também nos parâmetros de consulta.</summary>
    internal static bool? Flag(string text) => text.Trim().ToLowerInvariant() switch
    {
        "true" or "1" or "yes" or "on" or "t" or "y" => true,
        "false" or "0" or "no" or "off" or "f" or "n" => false,
        _ => null,
    };

    /// <summary>Recusa com 422 tudo o que foi encontrado.</summary>
    public void Done()
    {
        if (_object is null && _problems.Count == 0) _problems.Add("Corpo da requisição ausente.");
        if (_problems.Count > 0) throw HttpError.Validation(_problems);
    }

    private JsonNode? Field(string name)
    {
        if (_object is null || !_object.TryGetPropertyValue(name, out var node)) return null;
        return node;
    }

    private string? CheckText(string name, JsonNode node, int min, int max, string? pattern)
    {
        if (node is not JsonValue v || v.GetValueKind() != JsonValueKind.String)
        {
            _problems.Add($"{name}: precisa ser texto.");
            return null;
        }
        var text = v.GetValue<string>();
        // O pydantic conta caracteres, não bytes nem unidades UTF-16.
        var length = text.EnumerateRunes().Count();
        if (length < min || length > max)
        {
            _problems.Add($"{name}: precisa ter de {min} a {max} caracteres.");
            return null;
        }
        if (pattern is not null && !System.Text.RegularExpressions.Regex.IsMatch(text, pattern))
        {
            _problems.Add($"{name}: valor não aceito.");
            return null;
        }
        return text;
    }
}
