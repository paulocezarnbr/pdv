using Pdv.Core.Stock;

namespace Pdv.Core.Scale;

/// <summary>Preço do item pesado e desconto do caixa — o <c>pricing.py</c> do Python, em centavos inteiros.</summary>
/// <remarks>
/// Conferido contra <c>contracts/scale-weighing.json</c>. Os dois arredondamentos
/// são diferentes <b>de propósito</b>, porque são os do Python: o preço por peso
/// é "meio para cima" (<c>ROUND_HALF_UP</c>); o desconto do caixa usa o
/// <c>quantize</c> padrão, que é "meio para o par".
/// </remarks>
public static class WeightPricing
{
    public const int GramsPerKilo = 1000;

    /// <summary>Peso líquido = bruto − tara. Cobrar a embalagem é infração ao Inmetro, além de roubar o cliente.</summary>
    /// <exception cref="InvalidQuantityException">Bruto ou tara negativos, ou tara maior que o bruto.</exception>
    public static long NetWeight(long grossGrams, long tareGrams)
    {
        if (grossGrams < 0) throw new InvalidQuantityException("Peso bruto negativo: tare a balança");
        if (tareGrams < 0) throw new InvalidQuantityException("Tara negativa no cadastro do produto");
        if (tareGrams > grossGrams)
        {
            throw new InvalidQuantityException($"Tara ({tareGrams} g) maior que o peso bruto ({grossGrams} g)");
        }
        return grossGrams - tareGrams;
    }

    /// <summary>
    /// preço/kg × gramas ÷ 1000, arredondado <b>uma vez</b>, no fim:
    /// R$ 49,90/kg × 847 g = 4226,53 → 4227 centavos.
    /// </summary>
    public static long PriceForWeight(long priceCentsPerKg, long netGrams)
    {
        if (netGrams < 0) throw new InvalidQuantityException("Peso líquido negativo");
        if (priceCentsPerKg < 0) throw new InvalidQuantityException("Preço por quilo negativo no cadastro");
        return (long)Math.Round(priceCentsPerKg * (decimal)netGrams / GramsPerKilo, 0, MidpointRounding.AwayFromZero);
    }

    /// <summary>O desconto percentual do caixa sobre o subtotal, em centavos (meio para o par, como o Python).</summary>
    public static long DiscountFor(long subtotalCents, decimal percent)
    {
        if (percent < 0 || percent > 100) throw new InvalidQuantityException("Desconto precisa estar entre 0% e 100%");
        return (long)Math.Round(subtotalCents * percent / 100m, 0, MidpointRounding.ToEven);
    }

    /// <summary>"0,847 kg" — só para exibição.</summary>
    public static string Kilos(long grams) =>
        (grams / (decimal)GramsPerKilo).ToString("0.000", System.Globalization.CultureInfo.GetCultureInfo("pt-BR")) + " kg";
}
