using Pdv.Data;

namespace Pdv.Core.Tests;

/// <summary>Catálogo de teste: uma fatia com ficha técnica, um refrigerante sem ficha, um bolo por quilo sem ficha e uma torta por quilo com ficha.</summary>
public static class Fixtures
{
    public static void SeedCatalog(PdvDatabase database)
    {
        // Produto e ficha apontam um para o outro: a checagem das chaves fica
        // para o fim da transação, como numa carga de catálogo real.
        database.Execute(
            """
            BEGIN;
            PRAGMA defer_foreign_keys = ON;
            INSERT INTO inventory_items (id, tenant_id, store_id, name, unit, balance_mg, min_stock_mg, avg_cost_cents_per_kg, updated_at)
            VALUES ('farinha', 'tenant-1', 'store-1', 'Farinha', 'mg', 1000000, 0, 520, 'x'),
                   ('chocolate', 'tenant-1', 'store-1', 'Chocolate', 'mg', 10000, 0, 4890, 'x');
            INSERT INTO recipes (id, tenant_id, product_id, base_qty_g, yield_factor, updated_at)
            VALUES ('r-fatia', 'tenant-1', 'p-fatia', 120, '0.85', 'x'),
                   ('r-torta', 'tenant-1', 'p-torta-kg', 1000, '1', 'x');
            INSERT INTO recipe_lines (id, recipe_id, inventory_item_id, qty_per_base_mg, waste_percent, updated_at)
            VALUES ('l1', 'r-fatia', 'farinha', 30000, '0', 'x'), ('l2', 'r-fatia', 'chocolate', 40000, '2.5', 'x'),
                   ('l3', 'r-torta', 'farinha', 250000, '0', 'x'), ('l4', 'r-torta', 'chocolate', 32000, '2.5', 'x');
            INSERT INTO products (id, tenant_id, store_id, sku, barcode, name, category, pricing_mode, price_cents, tare_grams, recipe_id, is_active, updated_at)
            VALUES ('p-fatia', 'tenant-1', 'store-1', 'F1', '7890000000011', 'Fatia de torta', 'Doces', 'unit', 1450, 0, 'r-fatia', 1, 'x'),
                   ('p-refri', 'tenant-1', 'store-1', 'R1', '7890000000028', 'Refrigerante lata', 'Bebidas', 'unit', 333, 0, NULL, 1, 'x'),
                   ('p-kg', 'tenant-1', 'store-1', 'K1', NULL, 'Bolo por quilo', 'Doces', 'weight', 8990, 0, NULL, 1, 'x'),
                   ('p-torta-kg', 'tenant-1', 'store-1', 'T1', NULL, 'Mousse a granel', 'Doces', 'weight', 4990, 0, 'r-torta', 1, 'x');
            COMMIT;
            """);
    }
}
