"""Repositórios tipados sobre o SQLite local.

Todo método de escrita recebe a `sqlite3.Connection` da transação em andamento
— nunca abre transação própria. Isso é o que permite ao `CheckoutService`
compor venda + estoque + auditoria + outbox num único `COMMIT`.

Toda escrita de entidade sincronizável também enfileira no `sync_outbox`
**dentro da mesma transação**. Se a linha existe, o envio existe: é impossível
gravar uma venda e esquecer de sincronizá-la.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from pdv.domain.errors import RecipeNotFoundError
from pdv.domain.models import (
    Cents,
    EntityId,
    Grams,
    IngredientConsumption,
    Milligrams,
    PricingMode,
    Product,
    Recipe,
    RecipeLine,
    SaleItem,
    iso,
    new_id,
    utc_now,
)


class OutboxRepository:
    """Fila de saída — o coração da garantia offline (ver der.md §3)."""

    def enqueue(
        self,
        connection: sqlite3.Connection,
        *,
        entity_table: str,
        entity_id: EntityId,
        client_uuid: EntityId,
        operation: str,
        payload: dict[str, object],
    ) -> None:
        now = iso(utc_now())
        connection.execute(
            """
            INSERT INTO sync_outbox
                (entity_table, entity_id, client_uuid, operation,
                 payload_json, available_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_table,
                entity_id,
                client_uuid,
                operation,
                # sort_keys: payload determinístico facilita depurar e comparar
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                now,
                now,
            ),
        )

    def pending_count(self, connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT COUNT(*) AS total FROM sync_outbox").fetchone()
        return int(row["total"])


class ProductRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def list_active(self, tenant_id: EntityId) -> list[Product]:
        rows = self._connection.execute(
            """
            SELECT * FROM products
            WHERE tenant_id = ? AND is_active = 1 AND deleted_at IS NULL
            ORDER BY name
            """,
            (tenant_id,),
        ).fetchall()
        return [self._to_product(row) for row in rows]

    def get(self, product_id: EntityId) -> Product | None:
        row = self._connection.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        return self._to_product(row) if row else None

    def find_by_barcode(self, barcode: str) -> Product | None:
        row = self._connection.execute(
            "SELECT * FROM products WHERE barcode = ? AND is_active = 1", (barcode,)
        ).fetchone()
        return self._to_product(row) if row else None

    @staticmethod
    def _to_product(row: sqlite3.Row) -> Product:
        return Product(
            id=EntityId(row["id"]),
            tenant_id=EntityId(row["tenant_id"]),
            sku=row["sku"],
            name=row["name"],
            pricing_mode=PricingMode(row["pricing_mode"]),
            price_cents=Cents(int(row["price_cents"])),
            tare_grams=Grams(int(row["tare_grams"])),
            recipe_id=EntityId(row["recipe_id"]) if row["recipe_id"] else None,
        )


class RecipeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get_for_product_or_none(self, product: Product) -> Recipe | None:
        """Ficha do produto, ou `None` quando ele simplesmente não tem uma.

        Existe para o item **unitário**, onde a ausência de ficha é legítima: um
        refrigerante de revenda não tem receita, e exigir uma impediria a venda.
        Para o pesável, a ausência continua sendo erro de cadastro — lá vale
        `get_for_product`, que levanta.
        """
        if product.recipe_id is None:
            return None
        try:
            return self.get_for_product(product)
        except RecipeNotFoundError:
            return None

    def get_for_product(self, product: Product) -> Recipe:
        """Ficha técnica do produto.

        Raises:
            RecipeNotFoundError: produto pesável sem ficha é erro de cadastro —
                vender assim significa estoque que nunca baixa e CMV fantasma.
        """
        if product.recipe_id is None:
            raise RecipeNotFoundError(
                f"Produto {product.name!r} não possui ficha técnica cadastrada"
            )

        header = self._connection.execute(
            "SELECT * FROM recipes WHERE id = ?", (product.recipe_id,)
        ).fetchone()
        if header is None:
            raise RecipeNotFoundError(
                f"Ficha técnica {product.recipe_id} não encontrada no banco local"
            )

        lines = self._connection.execute(
            """
            SELECT rl.*, ii.name AS item_name, ii.avg_cost_cents_per_kg AS item_cost
            FROM recipe_lines rl
            JOIN inventory_items ii ON ii.id = rl.inventory_item_id
            WHERE rl.recipe_id = ?
            ORDER BY ii.name
            """,
            (product.recipe_id,),
        ).fetchall()

        return Recipe(
            id=EntityId(header["id"]),
            product_id=EntityId(header["product_id"]),
            base_qty_g=Grams(int(header["base_qty_g"])),
            yield_factor=Decimal(str(header["yield_factor"])),
            lines=tuple(
                RecipeLine(
                    inventory_item_id=EntityId(row["inventory_item_id"]),
                    inventory_item_name=row["item_name"],
                    qty_per_base_mg=Milligrams(int(row["qty_per_base_mg"])),
                    waste_percent=Decimal(str(row["waste_percent"])),
                    unit_cost_cents_per_kg=Cents(int(row["item_cost"])),
                )
                for row in lines
            ),
        )


class StockRepository:
    """Movimentos de estoque. Append-only; o saldo é derivado."""

    def __init__(self, connection: sqlite3.Connection, outbox: OutboxRepository) -> None:
        self._connection = connection
        self._outbox = outbox

    def balance_mg(self, inventory_item_id: EntityId) -> Milligrams:
        row = self._connection.execute(
            "SELECT balance_mg FROM inventory_items WHERE id = ?", (inventory_item_id,)
        ).fetchone()
        return Milligrams(int(row["balance_mg"])) if row else Milligrams(0)

    def recompute_balance(self, inventory_item_id: EntityId) -> Milligrams:
        """Recalcula o saldo a partir dos movimentos — a fonte da verdade.

        Usado no inventário e sempre que houver suspeita de divergência do cache.
        """
        row = self._connection.execute(
            "SELECT COALESCE(SUM(qty_mg), 0) AS total FROM stock_movements "
            "WHERE inventory_item_id = ?",
            (inventory_item_id,),
        ).fetchone()
        return Milligrams(int(row["total"]))

    def register_movement(
        self,
        *,
        tenant_id: EntityId,
        store_id: EntityId,
        device_id: EntityId,
        inventory_item_id: EntityId,
        qty_mg: Milligrams,
        movement_type: str,
        reference_type: str,
        reference_id: EntityId,
        unit_cost_cents: Cents = Cents(0),
    ) -> EntityId:
        """Grava o movimento e atualiza o cache de saldo, na mesma transação."""
        movement_id = new_id()
        client_uuid = new_id()
        now = iso(utc_now())

        self._connection.execute(
            """
            INSERT INTO stock_movements
                (id, tenant_id, store_id, inventory_item_id, qty_mg, movement_type,
                 reference_type, reference_id, unit_cost_cents, created_at,
                 origin_device_id, client_uuid, is_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                movement_id, tenant_id, store_id, inventory_item_id, int(qty_mg),
                movement_type, reference_type, reference_id, int(unit_cost_cents),
                now, device_id, client_uuid,
            ),
        )

        self._connection.execute(
            "UPDATE inventory_items SET balance_mg = balance_mg + ?, updated_at = ? "
            "WHERE id = ?",
            (int(qty_mg), now, inventory_item_id),
        )

        self._outbox.enqueue(
            self._connection,
            entity_table="stock_movements",
            entity_id=movement_id,
            client_uuid=client_uuid,
            operation="insert",
            payload={
                "id": movement_id,
                "tenant_id": tenant_id,
                "store_id": store_id,
                "inventory_item_id": inventory_item_id,
                "qty_mg": int(qty_mg),
                "movement_type": movement_type,
                "reference_type": reference_type,
                "reference_id": reference_id,
                "unit_cost_cents": int(unit_cost_cents),
                "created_at": now,
                "origin_device_id": device_id,
                "client_uuid": client_uuid,
            },
        )
        return EntityId(movement_id)


class SaleRepository:
    """Pedidos, itens e a foto dos ingredientes consumidos."""

    def __init__(self, connection: sqlite3.Connection, outbox: OutboxRepository) -> None:
        self._connection = connection
        self._outbox = outbox

    def create_order(
        self,
        *,
        order_id: EntityId,
        client_uuid: EntityId,
        tenant_id: EntityId,
        store_id: EntityId,
        device_id: EntityId,
        operator_id: EntityId,
        local_number: int,
        channel: str = "counter",
        origin_device_id: EntityId | None = None,
    ) -> None:
        """Cria o pedido.

        `origin_device_id` distingue **quem lançou** de **onde está gravado**.
        Um pedido do garçom nasce no celular e é gravado no PDV; manter a origem
        é o que permite ao relatório dizer de qual aparelho saiu cada venda — e
        ao módulo anti-furto separar o que veio do balcão do que veio do salão.
        """
        now = iso(utc_now())
        self._connection.execute(
            """
            INSERT INTO orders
                (id, tenant_id, store_id, device_id, local_number, channel, status,
                 operator_id, opened_at, created_at, updated_at, origin_device_id,
                 client_uuid, is_synced)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                order_id, tenant_id, store_id, device_id, local_number, channel,
                operator_id, now, now, now, origin_device_id or device_id,
                client_uuid,
            ),
        )

    def add_item(
        self,
        item: SaleItem,
        *,
        order_id: EntityId,
        tenant_id: EntityId,
        created_by_user_id: EntityId | None = None,
    ) -> None:
        now = iso(item.created_at)
        self._connection.execute(
            """
            INSERT INTO order_items
                (id, order_id, tenant_id, product_id, product_name, pricing_mode,
                 quantity, gross_weight_grams, tare_grams, net_weight_grams,
                 unit_price_cents, total_cents, scale_reading_raw, created_at,
                 created_by_user_id, client_uuid, is_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                item.id, order_id, tenant_id, item.product_id, item.product_name,
                item.pricing_mode.value, str(item.quantity),
                int(item.gross_weight_grams), int(item.tare_grams),
                int(item.net_weight_grams), int(item.unit_price_cents),
                int(item.total_cents), item.scale_reading_raw, now,
                created_by_user_id, item.client_uuid,
            ),
        )

        # Os ids ficam guardados para irem no payload: a nuvem grava a linha com
        # o MESMO id e `client_uuid` que ela tem aqui, e o reenvio cai no mesmo
        # `ON CONFLICT` em vez de virar um segundo consumo.
        ingredients: list[dict[str, object]] = []
        for consumption in item.consumptions:
            ingredient_id, ingredient_uuid = new_id(), new_id()
            self._connection.execute(
                """
                INSERT INTO order_item_ingredients
                    (id, order_item_id, inventory_item_id, inventory_item_name,
                     consumed_mg, unit_cost_cents, created_at, client_uuid, is_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    ingredient_id, item.id, consumption.inventory_item_id,
                    consumption.inventory_item_name, int(consumption.consumed_mg),
                    int(consumption.unit_cost_cents), now, ingredient_uuid,
                ),
            )
            ingredients.append({
                "id": ingredient_id,
                "client_uuid": ingredient_uuid,
                "inventory_item_id": consumption.inventory_item_id,
                "inventory_item_name": consumption.inventory_item_name,
                "consumed_mg": int(consumption.consumed_mg),
                "unit_cost_cents": int(consumption.unit_cost_cents),
            })

        self._outbox.enqueue(
            self._connection,
            entity_table="order_items",
            entity_id=item.id,
            client_uuid=item.client_uuid,
            operation="insert",
            payload={
                "id": item.id,
                "order_id": order_id,
                "tenant_id": tenant_id,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "pricing_mode": item.pricing_mode.value,
                "quantity": str(item.quantity),
                "gross_weight_grams": int(item.gross_weight_grams),
                "tare_grams": int(item.tare_grams),
                "net_weight_grams": int(item.net_weight_grams),
                "unit_price_cents": int(item.unit_price_cents),
                "total_cents": int(item.total_cents),
                "scale_reading_raw": item.scale_reading_raw,
                "created_at": now,
                "client_uuid": item.client_uuid,
                "created_by_user_id": created_by_user_id,
                "ingredients": ingredients,
            },
        )

    def cancel_item(
        self,
        item_id: EntityId,
        *,
        canceled_at: str,
        canceled_by_user_id: EntityId,
        reason: str,
    ) -> bool:
        """Marca o item cancelado E avisa a nuvem, na mesma transação.

        Os três caminhos de cancelamento (balcão, comanda inteira, comando
        remoto) só faziam o `UPDATE` local. O item seguia vivo na nuvem: entrava
        no "mais vendidos" do painel e nas sugestões do cardápio, que existem
        justamente para ignorar o que foi cancelado. Um lugar só para os três.

        Devolve `False` se o item já estava cancelado — o primeiro cancelamento
        é o que vale, porque é ele que tem quem autorizou.
        """
        changed = self._connection.execute(
            "UPDATE order_items SET canceled_at = ?, canceled_by_user_id = ?, "
            "cancel_reason = ? WHERE id = ? AND canceled_at IS NULL",
            (canceled_at, canceled_by_user_id, reason, item_id),
        ).rowcount
        if not changed:
            return False
        self._outbox.enqueue(
            self._connection,
            entity_table="order_items",
            entity_id=item_id,
            # `client_uuid` novo: é uma mudança, não o item de novo. Com o do
            # item, a nuvem a leria como reenvio e descartaria.
            client_uuid=EntityId(new_id()),
            operation="update",
            payload={
                "id": item_id,
                "canceled_at": canceled_at,
                "canceled_by_user_id": canceled_by_user_id,
                "cancel_reason": reason,
            },
        )
        return True

    def update_totals(
        self, order_id: EntityId, subtotal: Cents, discount: Cents, total: Cents
    ) -> None:
        self._connection.execute(
            "UPDATE orders SET subtotal_cents = ?, discount_cents = ?, "
            "total_cents = ?, updated_at = ? WHERE id = ?",
            (int(subtotal), int(discount), int(total), iso(utc_now()), order_id),
        )

    def close_order(
        self,
        *,
        order_id: EntityId,
        client_uuid: EntityId,
        tenant_id: EntityId,
        store_id: EntityId,
        device_id: EntityId,
        subtotal: Cents,
        discount: Cents,
        total: Cents,
    ) -> None:
        now = iso(utc_now())
        self._connection.execute(
            "UPDATE orders SET status = 'paid', closed_at = ?, updated_at = ?, "
            "subtotal_cents = ?, discount_cents = ?, total_cents = ? WHERE id = ?",
            (now, now, int(subtotal), int(discount), int(total), order_id),
        )
        # A nuvem exige o número da venda e só por ele o cupom na mão do cliente
        # é achado no painel. Até a 1.1.2 ele não ia, e o lote inteiro abortava.
        opened = self._connection.execute(
            "SELECT local_number, channel, operator_id, opened_at FROM orders "
            "WHERE id = ?",
            (order_id,),
        ).fetchone()
        self._outbox.enqueue(
            self._connection,
            entity_table="orders",
            entity_id=order_id,
            client_uuid=client_uuid,
            operation="insert",
            payload={
                "id": order_id,
                "tenant_id": tenant_id,
                "store_id": store_id,
                "device_id": device_id,
                "status": "paid",
                "local_number": int(opened["local_number"]),
                "channel": opened["channel"],
                "operator_id": opened["operator_id"],
                "opened_at": opened["opened_at"],
                "subtotal_cents": int(subtotal),
                "discount_cents": int(discount),
                "total_cents": int(total),
                "closed_at": now,
                "client_uuid": client_uuid,
            },
        )


class IngredientSnapshot:
    """Helper de leitura para a tela de conferência de baixa."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def for_item(self, order_item_id: EntityId) -> list[IngredientConsumption]:
        rows = self._connection.execute(
            "SELECT * FROM order_item_ingredients WHERE order_item_id = ?",
            (order_item_id,),
        ).fetchall()
        return [
            IngredientConsumption(
                inventory_item_id=EntityId(row["inventory_item_id"]),
                inventory_item_name=row["inventory_item_name"],
                consumed_mg=Milligrams(int(row["consumed_mg"])),
                unit_cost_cents=Cents(int(row["unit_cost_cents"])),
            )
            for row in rows
        ]
