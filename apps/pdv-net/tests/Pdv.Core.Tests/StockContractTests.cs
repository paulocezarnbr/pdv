using System.Globalization;
using System.Text.Json;
using Pdv.Core.Stock;

namespace Pdv.Core.Tests;

/// <summary>A baixa de estoque contra <c>contracts/recipe-explosion.json</c>, gerado pelo Python.</summary>
public sealed class StockContractTests
{
    private static readonly JsonElement Contract =
        JsonDocument.Parse(File.ReadAllText(TestDatabase.Contract("recipe-explosion.json"))).RootElement.Clone();

    public static TheoryData<int> Cases()
    {
        var data = new TheoryData<int>();
        for (var i = 0; i < Contract.GetProperty("cases").GetArrayLength(); i++) data.Add(i);
        return data;
    }

    private static Recipe RecipeOf(JsonElement spec) => new(
        "r-" + spec.GetProperty("name").GetString(),
        "p-" + spec.GetProperty("name").GetString(),
        spec.GetProperty("base_qty_g").GetInt64(),
        decimal.Parse(spec.GetProperty("yield_factor").GetString()!, CultureInfo.InvariantCulture),
        spec.GetProperty("lines").EnumerateArray().Select(line => new RecipeLine(
            line.GetProperty("inventory_item_id").GetString()!,
            line.GetProperty("inventory_item_name").GetString()!,
            line.GetProperty("qty_per_base_mg").GetInt64(),
            decimal.Parse(line.GetProperty("waste_percent").GetString()!, CultureInfo.InvariantCulture),
            line.GetProperty("unit_cost_cents_per_kg").GetInt64())).ToList());

    [Theory]
    [MemberData(nameof(Cases))]
    public void Every_milligram_matches_the_python(int index)
    {
        var item = Contract.GetProperty("cases")[index];
        var actual = RecipeExplosion.Explode(RecipeOf(item.GetProperty("recipe")), item.GetProperty("sold_grams").GetInt64());
        var expected = item.GetProperty("consumptions").EnumerateArray()
            .Select(c => (c.GetProperty("inventory_item_id").GetString()!, c.GetProperty("consumed_mg").GetInt64(),
                c.GetProperty("unit_cost_cents").GetInt64()))
            .ToList();
        Assert.Equal(expected, actual.Select(c => (c.InventoryItemId, c.ConsumedMg, c.UnitCostCents)).ToList());
    }

    [Fact]
    public void Unit_totals_round_half_to_even_like_python()
    {
        foreach (var item in Contract.GetProperty("unit_totals").EnumerateArray())
        {
            var quantity = decimal.Parse(item.GetProperty("quantity").GetString()!, CultureInfo.InvariantCulture);
            Assert.Equal(
                item.GetProperty("total_cents").GetInt64(),
                RecipeExplosion.UnitTotal(item.GetProperty("price_cents").GetInt64(), quantity));
        }
    }

    [Fact]
    public void Nothing_is_written_off_for_zero_grams() =>
        Assert.Throws<InvalidQuantityException>(() =>
            RecipeExplosion.Explode(new Recipe("r", "p", 1000, 1m, []), 0));
}
