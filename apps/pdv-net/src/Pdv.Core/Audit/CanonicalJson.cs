using System.Collections;
using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Json;

namespace Pdv.Core.Audit;

/// <summary>
/// JSON canônico idêntico, byte a byte, ao do PDV em Python:
/// <c>json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)</c>.
/// </summary>
/// <remarks>
/// <para>
/// Durante a transição os dois PDVs gravam e verificam o mesmo
/// <c>audit_ledger</c>. O HMAC de cada elo é calculado sobre este texto: um
/// byte diferente — uma chave em outra ordem, um "ç" escapado, um
/// <c>1E-05</c> no lugar de <c>1e-05</c> — e a verificação acusa adulteração
/// onde não houve nenhuma. O caixa não abre.
/// </para>
/// <para>
/// A referência é <c>contracts/audit-chain.json</c>, gerado pela implementação
/// Python. Os testes exigem igualdade com cada vetor.
/// </para>
/// </remarks>
public static class CanonicalJson
{
    public static string Serialize(object? value) => Serialize(value, Compact);

    /// <summary>
    /// O <c>json.dumps(..., sort_keys=True)</c> SEM <c>separators</c>: <c>", "</c> e
    /// <c>": "</c>. É o formato do <c>payload_json</c> do outbox no Python. Não
    /// entra em hash nenhum, mas os dois PDVs gravam a mesma fila, e uma fila com
    /// dois formatos é uma fila que ninguém consegue comparar ao depurar.
    /// </summary>
    public static string SerializeForOutbox(object? value) => Serialize(value, PythonDefault);

    private sealed record Separators(string Item, string Key);

    private static readonly Separators Compact = new(",", ":");
    private static readonly Separators PythonDefault = new(", ", ": ");

    private static string Serialize(object? value, Separators separators)
    {
        var builder = new StringBuilder();
        Write(builder, value, separators);
        return builder.ToString();
    }

    private static void Write(StringBuilder output, object? value, Separators separators)
    {
        switch (value)
        {
            case null:
                output.Append("null");
                break;
            case bool flag:
                output.Append(flag ? "true" : "false");
                break;
            case string text:
                WriteString(output, text);
                break;
            case JsonElement element:
                WriteElement(output, element, separators);
                break;
            case double number:
                output.Append(PythonFloat.Repr(number));
                break;
            case float number:
                output.Append(PythonFloat.Repr(number));
                break;
            case decimal money:
                // `default=str` do Python: Decimal vira TEXTO ("12.50"), não número.
                WriteString(output, money.ToString(CultureInfo.InvariantCulture));
                break;
            case sbyte or byte or short or ushort or int or uint or long or ulong or BigInteger:
                output.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
                break;
            case DateTime or DateTimeOffset:
                // O `str()` de datetime do Python ("2026-09-25 03:49:25.134000+00:00")
                // não é o ISO gravado no banco. Data entra no payload já como texto.
                throw new ArgumentException(
                    "Data no payload de auditoria precisa ir como texto ISO (Iso.Format).");
            case IDictionary map:
                WriteObject(output, map, separators);
                break;
            case IEnumerable sequence:
                WriteArray(output, sequence, separators);
                break;
            default:
                // `default=str`: Guid e afins viram o texto deles.
                WriteString(output, Convert.ToString(value, CultureInfo.InvariantCulture) ?? "");
                break;
        }
    }

    private static void WriteObject(StringBuilder output, IDictionary map, Separators separators)
    {
        var entries = new List<KeyValuePair<string, object?>>(map.Count);
        foreach (DictionaryEntry entry in map)
        {
            if (entry.Key is not string key)
            {
                throw new ArgumentException("Chave de payload de auditoria precisa ser texto.");
            }
            entries.Add(new(key, entry.Value));
        }
        entries.Sort((a, b) => CodePointComparer.Instance.Compare(a.Key, b.Key));

        output.Append('{');
        for (var i = 0; i < entries.Count; i++)
        {
            if (i > 0) output.Append(separators.Item);
            WriteString(output, entries[i].Key);
            output.Append(separators.Key);
            Write(output, entries[i].Value, separators);
        }
        output.Append('}');
    }

    private static void WriteArray(StringBuilder output, IEnumerable sequence, Separators separators)
    {
        output.Append('[');
        var first = true;
        foreach (var item in sequence)
        {
            if (!first) output.Append(separators.Item);
            first = false;
            Write(output, item, separators);
        }
        output.Append(']');
    }

    private static void WriteElement(StringBuilder output, JsonElement element, Separators separators)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                var map = new Dictionary<string, object?>(StringComparer.Ordinal);
                foreach (var property in element.EnumerateObject())
                {
                    map[property.Name] = property.Value;
                }
                WriteObject(output, map, separators);
                break;
            case JsonValueKind.Array:
                WriteArray(output, element.EnumerateArray().Select(item => (object?)item), separators);
                break;
            case JsonValueKind.String:
                WriteString(output, element.GetString()!);
                break;
            case JsonValueKind.Number:
                // O `json.loads` do Python decide pelo literal: com ponto ou
                // expoente é float; sem, é inteiro de precisão arbitrária.
                var raw = element.GetRawText();
                if (raw.AsSpan().IndexOfAny('.', 'e', 'E') >= 0)
                {
                    output.Append(PythonFloat.Repr(double.Parse(raw, CultureInfo.InvariantCulture)));
                }
                else
                {
                    output.Append(BigInteger.Parse(raw, CultureInfo.InvariantCulture)
                        .ToString(CultureInfo.InvariantCulture));
                }
                break;
            case JsonValueKind.True:
                output.Append("true");
                break;
            case JsonValueKind.False:
                output.Append("false");
                break;
            default:
                output.Append("null");
                break;
        }
    }

    /// <summary>Escapes do <c>json.dumps</c> com <c>ensure_ascii=False</c>.</summary>
    private static void WriteString(StringBuilder output, string text)
    {
        output.Append('"');
        foreach (var ch in text)
        {
            switch (ch)
            {
                case '"': output.Append("\\\""); break;
                case '\\': output.Append("\\\\"); break;
                case '\n': output.Append("\\n"); break;
                case '\r': output.Append("\\r"); break;
                case '\t': output.Append("\\t"); break;
                case '\b': output.Append("\\b"); break;
                case '\f': output.Append("\\f"); break;
                default:
                    if (ch < 0x20)
                    {
                        // Python usa hexadecimal minúsculo: \u001f, não \u001F.
                        output.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        // Acento, emoji e DEL (0x7f) saem crus, como no Python.
                        output.Append(ch);
                    }
                    break;
            }
        }
        output.Append('"');
    }

    /// <summary>
    /// Ordem do <c>sort_keys</c> do Python: por code point. A comparação
    /// ordinal do .NET é por unidade UTF-16 e só diverge com caracteres fora
    /// do plano básico, mas "só diverge às vezes" é exatamente o tipo de
    /// diferença que quebra a cadeia sem ninguém entender por quê.
    /// </summary>
    private sealed class CodePointComparer : IComparer<string>
    {
        public static readonly CodePointComparer Instance = new();

        public int Compare(string? x, string? y)
        {
            var left = (x ?? "").EnumerateRunes();
            var right = (y ?? "").EnumerateRunes();
            while (true)
            {
                var hasLeft = left.MoveNext();
                var hasRight = right.MoveNext();
                if (!hasLeft || !hasRight) return hasLeft.CompareTo(hasRight);
                var difference = left.Current.Value.CompareTo(right.Current.Value);
                if (difference != 0) return difference;
            }
        }
    }
}
