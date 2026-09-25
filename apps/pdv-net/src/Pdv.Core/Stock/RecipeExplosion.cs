namespace Pdv.Core.Stock;

public sealed record RecipeLine(
    string InventoryItemId, string InventoryItemName, long QtyPerBaseMg, decimal WastePercent, long UnitCostCentsPerKg);

public sealed record Recipe(string Id, string ProductId, long BaseQtyG, decimal YieldFactor, IReadOnlyList<RecipeLine> Lines);

public sealed record IngredientConsumption(
    string InventoryItemId, string InventoryItemName, long ConsumedMg, long UnitCostCents);

public sealed class InvalidQuantityException(string message) : Exception(message);

/// <summary>A baixa fracionada por ficha técnica — o <c>explode_recipe</c> do Python.</summary>
/// <remarks>
/// <para>
/// O cliente leva 847 g de torta; a ficha é para 1000 g. Baixa-se a fração
/// exata de cada insumo, não "1 torta". Tudo em miligramas inteiros; o
/// <c>decimal</c> só nos fatores, e o arredondamento acontece <b>uma vez</b>, no fim.
/// </para>
/// <para>
/// Conferido contra <c>contracts/recipe-explosion.json</c>, gerado pelo Python:
/// um miligrama de diferença por venda é um estoque que diverge da nuvem em
/// semanas, sem erro nenhum em lugar nenhum.
/// </para>
/// </remarks>
public static class RecipeExplosion
{
    /// <summary>
    /// consumo_mg = qty_por_base_mg × (vendido / base) × (1 + perda/100) ÷ rendimento,
    /// arredondado "meio para cima" (ROUND_HALF_UP) no fim.
    /// </summary>
    /// <remarks>
    /// Rendimento menor que 1 é produto que encolhe no forno: para entregar
    /// 847 g assados é preciso consumir MAIS insumo cru — por isso divide.
    /// </remarks>
    public static IReadOnlyList<IngredientConsumption> Explode(Recipe recipe, long soldGrams)
    {
        if (soldGrams <= 0) throw new InvalidQuantityException("Peso vendido deve ser maior que zero");
        if (recipe.BaseQtyG <= 0) throw new InvalidQuantityException($"Ficha técnica {recipe.Id} com rendimento-base inválido");
        if (recipe.YieldFactor <= 0) throw new InvalidQuantityException($"Ficha técnica {recipe.Id} com fator de rendimento inválido");

        var proportion = (decimal)soldGrams / recipe.BaseQtyG;
        return recipe.Lines.Select(line =>
        {
            var waste = 1m + line.WastePercent / 100m;
            var exact = line.QtyPerBaseMg * proportion * waste / recipe.YieldFactor;
            var consumed = (long)Math.Round(exact, 0, MidpointRounding.AwayFromZero);
            return new IngredientConsumption(
                line.InventoryItemId, line.InventoryItemName, consumed, CostFor(line.UnitCostCentsPerKg, consumed));
        }).ToList();
    }

    /// <summary>Custo do insumo consumido, para o CMV.</summary>
    public static long CostFor(long costCentsPerKg, long consumedMg) =>
        (long)Math.Round(costCentsPerKg * (decimal)consumedMg / 1_000_000m, 0, MidpointRounding.AwayFromZero);

    /// <summary>
    /// Total do item por unidade: preço × quantidade, arredondado uma vez.
    /// </summary>
    /// <remarks>
    /// "Meio para o par" (ROUND_HALF_EVEN), como o <c>quantize</c> padrão do
    /// <c>Decimal</c> do Python: 1,5 × R$ 3,33 = 499,5 centavos → 500;
    /// 1,5 × R$ 3,35 = 502,5 → 502. Arredondar para cima aqui daria um centavo a
    /// mais que o Python em metade dos casos de meio.
    /// </remarks>
    public static long UnitTotal(long priceCents, decimal quantity) =>
        (long)Math.Round(priceCents * quantity, 0, MidpointRounding.ToEven);

    /// <summary>A porção de um item unitário com ficha: base × quantidade (truncado, como o int() do Python).</summary>
    public static long UnitPortionGrams(long baseQtyG, decimal quantity) => (long)decimal.Truncate(baseQtyG * quantity);
}
