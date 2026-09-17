"""Dados de demonstração para rodar o PDV sem a nuvem.

Em produção estes registros chegam pelo *pull* de sincronização. Aqui eles
existem para que `python main.py` funcione numa máquina limpa — incluindo uma
confeitaria com ficha técnica real, que é o caso que exercita a baixa
fracionada.
"""

from __future__ import annotations

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.domain.models import iso, new_id, utc_now


def seed_demo_data(database: Database, config: AppConfig) -> None:
    """Popula o banco local se ainda não houver produtos. Idempotente."""
    connection = database.connection
    existing = connection.execute("SELECT COUNT(*) AS total FROM products").fetchone()
    if int(existing["total"]) > 0:
        return

    now = iso(utc_now())
    tenant, store = config.tenant_id, config.store_id

    with database.transaction() as tx:
        # -- operador e gerente (autorização offline) ------------------------ #
        operator_id = "44444444-4444-4444-4444-444444444444"
        manager_id = "55555555-5555-5555-5555-555555555555"
        tx.executemany(
            """
            INSERT INTO users (id, tenant_id, name, login, role, can_authorize,
                               max_discount_percent, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (operator_id, tenant, "Ana Caixa", "ana", "cashier", 0, "5", now),
                (manager_id, tenant, "Bruno Gerente", "bruno", "manager", 1, "30", now),
            ],
        )

        # -- insumos --------------------------------------------------------- #
        # Saldos em miligramas: 10 kg = 10_000_000 mg.
        ingredients = [
            (new_id(), "Farinha de trigo", 10_000_000, 650),
            (new_id(), "Acucar refinado", 8_000_000, 480),
            (new_id(), "Chocolate meio amargo", 5_000_000, 4_200),
            (new_id(), "Manteiga sem sal", 4_000_000, 5_800),
            (new_id(), "Ovos (pasteurizado)", 6_000_000, 1_900),
        ]
        tx.executemany(
            """
            INSERT INTO inventory_items
                (id, tenant_id, store_id, name, unit, balance_mg, min_stock_mg,
                 avg_cost_cents_per_kg, updated_at)
            VALUES (?, ?, ?, ?, 'mg', ?, 1000000, ?, ?)
            """,
            [
                (item_id, tenant, store, name, balance, cost, now)
                for item_id, name, balance, cost in ingredients
            ],
        )

        # -- produto pesável + ficha técnica ---------------------------------- #
        product_id = new_id()
        recipe_id = new_id()

        tx.execute(
            """
            INSERT INTO products
                (id, tenant_id, store_id, sku, barcode, name, category, pricing_mode,
                 price_cents, tare_grams, recipe_id, is_active, updated_at)
            VALUES (?, ?, ?, 'TORTA-CHOC', '2000001000009',
                    'Torta de Chocolate (kg)', 'Confeitaria', 'weight',
                    8990, 45, ?, 1, ?)
            """,
            (product_id, tenant, store, recipe_id, now),
        )

        # Ficha para 1000 g de torta pronta. yield_factor 0.92: o produto perde
        # 8% de peso no forno, então consome mais insumo cru do que entrega.
        tx.execute(
            """
            INSERT INTO recipes (id, tenant_id, product_id, base_qty_g,
                                 yield_factor, updated_at)
            VALUES (?, ?, ?, 1000, '0.92', ?)
            """,
            (recipe_id, tenant, product_id, now),
        )

        recipe_lines = [
            (ingredients[0][0], 250_000, "2"),    # farinha  250 g, 2% de perda
            (ingredients[1][0], 180_000, "1"),    # açúcar   180 g
            (ingredients[2][0], 320_000, "3"),    # chocolate 320 g, 3% de perda
            (ingredients[3][0], 150_000, "1.5"),  # manteiga 150 g
            (ingredients[4][0], 200_000, "0"),    # ovos     200 g
        ]
        tx.executemany(
            """
            INSERT INTO recipe_lines
                (id, recipe_id, inventory_item_id, qty_per_base_mg,
                 waste_percent, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (new_id(), recipe_id, item_id, qty, waste, now)
                for item_id, qty, waste in recipe_lines
            ],
        )

        # -- segundo produto pesável, para testar troca de item --------------- #
        product2_id = new_id()
        recipe2_id = new_id()
        tx.execute(
            """
            INSERT INTO products
                (id, tenant_id, store_id, sku, barcode, name, category, pricing_mode,
                 price_cents, tare_grams, recipe_id, is_active, updated_at)
            VALUES (?, ?, ?, 'BOLO-CENOURA', '2000002000006',
                    'Bolo de Cenoura (kg)', 'Confeitaria', 'weight',
                    5490, 30, ?, 1, ?)
            """,
            (product2_id, tenant, store, recipe2_id, now),
        )
        tx.execute(
            """
            INSERT INTO recipes (id, tenant_id, product_id, base_qty_g,
                                 yield_factor, updated_at)
            VALUES (?, ?, ?, 1000, '0.95', ?)
            """,
            (recipe2_id, tenant, product2_id, now),
        )
        tx.executemany(
            """
            INSERT INTO recipe_lines
                (id, recipe_id, inventory_item_id, qty_per_base_mg,
                 waste_percent, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (new_id(), recipe2_id, ingredients[0][0], 300_000, "2", now),
                (new_id(), recipe2_id, ingredients[1][0], 220_000, "1", now),
                (new_id(), recipe2_id, ingredients[4][0], 180_000, "0", now),
            ],
        )


DEMO_OPERATOR_ID = "44444444-4444-4444-4444-444444444444"
DEMO_OPERATOR_NAME = "Ana Caixa"
DEMO_MANAGER_ID = "55555555-5555-5555-5555-555555555555"
