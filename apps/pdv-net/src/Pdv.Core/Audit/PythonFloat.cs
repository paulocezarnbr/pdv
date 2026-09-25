using System.Globalization;

namespace Pdv.Core.Audit;

/// <summary>O <c>repr(float)</c> do Python, que é o que o <c>json.dumps</c> escreve.</summary>
/// <remarks>
/// Os dois lados usam a menor representação que volta ao mesmo double, então os
/// DÍGITOS coincidem. O que muda é a forma: o .NET escreve <c>1E-05</c> e
/// <c>2</c>; o Python, <c>1e-05</c> e <c>2.0</c>. A regra do Python: notação
/// científica quando o expoente decimal é menor que -4 ou pelo menos 16; fixa,
/// sempre com parte decimal, no resto.
/// </remarks>
public static class PythonFloat
{
    public static string Repr(double value)
    {
        if (double.IsNaN(value)) return "NaN";
        if (double.IsPositiveInfinity(value)) return "Infinity";
        if (double.IsNegativeInfinity(value)) return "-Infinity";
        if (value == 0) return double.IsNegative(value) ? "-0.0" : "0.0";

        var shortest = value.ToString("R", CultureInfo.InvariantCulture);
        var negative = shortest[0] == '-';
        if (negative) shortest = shortest[1..];

        var exponent = 0;
        var mantissa = shortest;
        var marker = shortest.IndexOfAny(['E', 'e']);
        if (marker >= 0)
        {
            exponent = int.Parse(shortest[(marker + 1)..], NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture);
            mantissa = shortest[..marker];
        }

        var dot = mantissa.IndexOf('.');
        var integerPart = dot < 0 ? mantissa : mantissa[..dot];
        var fraction = dot < 0 ? "" : mantissa[(dot + 1)..];

        // dígitos significativos e a posição da vírgula relativa ao primeiro deles
        var digits = integerPart + fraction;
        var point = integerPart.Length + exponent;
        var leading = 0;
        while (leading < digits.Length - 1 && digits[leading] == '0') leading++;
        digits = digits[leading..];
        point -= leading;
        digits = digits.TrimEnd('0');
        if (digits.Length == 0) digits = "0";

        var scientific = point - 1;
        string text;
        if (scientific < -4 || scientific >= 16)
        {
            var head = digits.Length == 1 ? digits : digits[0] + "." + digits[1..];
            var sign = scientific < 0 ? "-" : "+";
            text = head + "e" + sign + Math.Abs(scientific).ToString("00", CultureInfo.InvariantCulture);
        }
        else if (point <= 0)
        {
            text = "0." + new string('0', -point) + digits;
        }
        else if (point >= digits.Length)
        {
            text = digits + new string('0', point - digits.Length) + ".0";
        }
        else
        {
            text = digits[..point] + "." + digits[point..];
        }

        return negative ? "-" + text : text;
    }
}
