using System.Globalization;
using Microsoft.Data.Sqlite;
using Pdv.Core.Stock;

namespace Pdv.Data.Sales;

public sealed record Product(
    string Id, string Name, string PricingMode, long PriceCents, long TareGrams, string? RecipeId, string? Barcode)
{
    public bool IsWeighed => PricingMode == "weight";
}

/// <summary>O catálogo que desce da nuvem — só leitura no caixa.</summary>
public sealed class Catalog(SqliteConnection connection, string tenantId)
{
    private const string Columns = "id, name, pricing_mode, price_cents, tare_grams, recipe_id, barcode";

    public Product? FindByBarcode(string barcode) =>
        Query(
            $"SELECT {Columns} FROM products WHERE tenant_id = $tenant AND barcode = $code " +
            "AND is_active = 1 AND deleted_at IS NULL LIMIT 1",
            ("$tenant", tenantId), ("$code", barcode.Trim())).FirstOrDefault();

    public Product? Get(string productId) =>
        Query($"SELECT {Columns} FROM products WHERE id = $id", ("$id", productId)).FirstOrDefault();

    /// <summary>Busca por nome ou código, para o operador que não tem o código de barras na mão.</summary>
    public IReadOnlyList<Product> Search(string text, int limit = 30)
    {
        var term = text.Trim();
        return Query(
            $"SELECT {Columns} FROM products WHERE tenant_id = $tenant AND is_active = 1 AND deleted_at IS NULL " +
            "AND ($term = '' OR name LIKE '%' || $term || '%' OR barcode = $term OR sku = $term) " +
            "ORDER BY name LIMIT $limit",
            ("$tenant", tenantId), ("$term", term), ("$limit", limit));
    }

    /// <summary>A ficha do produto, ou <c>null</c> — item unitário sem ficha (refrigerante de revenda) é legítimo.</summary>
    public Recipe? RecipeFor(Product product)
    {
        if (product.RecipeId is null) return null;

        long baseQty;
        decimal yieldFactor;
        using (var header = Sql.Command(connection, null,
                   "SELECT base_qty_g, yield_factor FROM recipes WHERE id = $id", ("$id", product.RecipeId)))
        using (var reader = header.ExecuteReader())
        {
            if (!reader.Read()) return null;
            baseQty = reader.GetInt64(0);
            yieldFactor = decimal.Parse(reader.GetValue(1).ToString()!, NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        var lines = new List<RecipeLine>();
        using (var command = Sql.Command(connection, null,
                   """
                   SELECT rl.inventory_item_id, ii.name, rl.qty_per_base_mg, rl.waste_percent, ii.avg_cost_cents_per_kg
                   FROM recipe_lines rl JOIN inventory_items ii ON ii.id = rl.inventory_item_id
                   WHERE rl.recipe_id = $id ORDER BY ii.name
                   """,
                   ("$id", product.RecipeId)))
        using (var reader = command.ExecuteReader())
        {
            while (reader.Read())
            {
                lines.Add(new RecipeLine(
                    reader.GetString(0), reader.GetString(1), reader.GetInt64(2),
                    decimal.Parse(reader.GetValue(3).ToString()!, NumberStyles.Float, CultureInfo.InvariantCulture),
                    reader.GetInt64(4)));
            }
        }
        return new Recipe(product.RecipeId, product.Id, baseQty, yieldFactor, lines);
    }

    private List<Product> Query(string sql, params (string Name, object? Value)[] parameters)
    {
        using var command = Sql.Command(connection, null, sql, parameters);
        using var reader = command.ExecuteReader();
        var products = new List<Product>();
        while (reader.Read())
        {
            products.Add(new Product(
                reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetInt64(3),
                reader.IsDBNull(4) ? 0 : reader.GetInt64(4),
                reader.IsDBNull(5) ? null : reader.GetString(5),
                reader.IsDBNull(6) ? null : reader.GetString(6)));
        }
        return products;
    }
}
