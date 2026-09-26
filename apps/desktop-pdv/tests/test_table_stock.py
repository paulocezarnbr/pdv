"""O item lançado na mesa baixa insumo, e o cancelamento estorna.

Até aqui `TableOrderService.add_item` gravava o item com `consumptions=()`: o
prato com ficha técnica saía da cozinha do salão sem mexer no estoque. O saldo
de insumo ficava acima do real, `order_item_ingredients` nunca recebia linha do
salão, e o CMV do painel da nuvem só enxergava o balcão.

A baixa é no **lançamento**, como no balcão: é quando o prato vai para a cozinha
e o insumo é gasto. Por isso todo cancelamento estorna — o da comanda inteira e
o remoto, pelo painel —, pelo consumo gravado e não pela ficha de hoje.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import (
    DEMO_MANAGER_ID,
    DEMO_MANAGER_NAME,
    DEMO_OPERATOR_ID,
    DEMO_OPERATOR_LOGIN,
    DEMO_OPERATOR_PIN,
    seed_demo_data,
)
from pdv.domain.models import EntityId, Payment, PaymentMethod, Cents, iso, new_id, utc_now
from pdv.edge.hub import EventHub
from pdv.edge.orders import ProductNotSellableError, TableOrderService
from pdv.edge.tables import TableService
from pdv.remote.commands import RemoteCommandService
from pdv.remote.inbox import InboxRepository
from pdv.remote.protocol import CommandKind, RemoteCommand, sign_command

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
PHONE = EntityId("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SECRET = b"segredo-do-terminal-provisionado-na-ativacao"


def _salon(tmp_path: Path, *, block: bool = False):  # noqa: ANN202
    config = AppConfig(
        tenant_id=TENANT,
        store_id=STORE,
        device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(block_sale_on_negative_stock=block),
        device_secret=SECRET,
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    hub = EventHub()
    return database, config, hub, TableOrderService(database, config, hub)


@pytest.fixture()
def salon(tmp_path: Path):  # noqa: ANN201
    return _salon(tmp_path)


def _product(database: Database, sku: str) -> EntityId:
    return EntityId(str(database.query_one("SELECT id FROM products WHERE sku = ?", (sku,))["id"]))


def _open(database: Database, config: AppConfig, orders: TableOrderService, label: str = "Mesa 3"):  # noqa: ANN202
    table = TableService(database, config).find_by_label(label)
    assert table is not None
    return orders.open_order(
        client_uuid=EntityId(new_id()),
        operator_id=EntityId(DEMO_OPERATOR_ID),
        table_id=table.id,
        origin_device_id=PHONE,
    )


def _add(orders: TableOrderService, order_id: EntityId, product_id: EntityId,
         quantity: str = "1", uuid: str | None = None) -> EntityId:
    """Lança e devolve o id do item — o último da comanda."""
    orders.add_item(
        order_id=order_id,
        client_uuid=EntityId(uuid or new_id()),
        product_id=product_id,
        quantity=Decimal(quantity),
    )
    return EntityId(str(orders.list_items(order_id)[-1]["id"]))


def _consumed(database: Database, item_id: str) -> dict[str, int]:
    return {
        str(row["inventory_item_id"]): int(row["consumed_mg"])
        for row in database.query_all(
            "SELECT inventory_item_id, consumed_mg FROM order_item_ingredients "
            " WHERE order_item_id = ?", (item_id,),
        )
    }


def _movements(database: Database, item_id: str) -> list[tuple[str, str, int]]:
    return [
        (str(r["movement_type"]), str(r["reference_type"]), int(r["qty_mg"]))
        for r in database.query_all(
            "SELECT movement_type, reference_type, qty_mg FROM stock_movements "
            " WHERE reference_id = ? ORDER BY rowid", (item_id,),
        )
    ]


def _balances(database: Database) -> dict[str, int]:
    return {
        str(r["id"]): int(r["balance_mg"])
        for r in database.query_all("SELECT id, balance_mg FROM inventory_items")
    }


# --------------------------------------------------------------------------- #
# A baixa
# --------------------------------------------------------------------------- #


def test_an_item_with_a_recipe_writes_off_stock_when_launched(salon) -> None:  # noqa: ANN001
    database, config, _, orders = salon
    before = _balances(database)
    order = _open(database, config, orders)

    item = _add(orders, order.id, _product(database, "FATIA-CHOC"), quantity="2")

    consumed = _consumed(database, item)
    assert len(consumed) == 5, "a fatia usa a ficha da torta: cinco insumos"
    assert all(mg > 0 for mg in consumed.values())
    # Uma saída `sale` por insumo, com o mesmo consumo gravado no item.
    assert sorted(_movements(database, item)) == sorted(
        ("sale", "order_item", -mg) for mg in consumed.values()
    )
    after = _balances(database)
    for key, mg in consumed.items():
        assert after[key] == before[key] - mg


def test_the_portion_follows_the_quantity(salon) -> None:  # noqa: ANN001
    database, config, _, orders = salon
    order = _open(database, config, orders)
    slice_id = _product(database, "FATIA-CHOC")

    one = _consumed(database, _add(orders, order.id, slice_id, quantity="1"))
    two = _consumed(database, _add(orders, order.id, slice_id, quantity="2"))
    assert one and one.keys() == two.keys()

    for key, mg in one.items():
        assert abs(two[key] - 2 * mg) <= 1  # um arredondamento só, no fim


def test_the_cloud_receives_the_ingredients_of_a_table_item(salon) -> None:  # noqa: ANN001
    """O CMV do painel sai do `ingredients` do item; vazio, a mesa custava zero."""
    database, config, _, orders = salon
    order = _open(database, config, orders)
    item = _add(orders, order.id, _product(database, "FATIA-CHOC"))

    row = database.query_one(
        "SELECT payload_json FROM sync_outbox WHERE entity_table = 'order_items' "
        " AND entity_id = ? AND operation = 'insert'", (item,),
    )
    ingredients = json.loads(row["payload_json"])["ingredients"]
    assert len(ingredients) == 5
    assert {i["inventory_item_id"]: i["consumed_mg"] for i in ingredients} == _consumed(database, item)
    assert all(i["unit_cost_cents"] >= 0 for i in ingredients)
    queued = database.query_one(
        "SELECT COUNT(*) AS n FROM sync_outbox WHERE entity_table = 'stock_movements'"
    )
    assert int(queued["n"]) == len(ingredients)


def test_an_item_without_a_recipe_moves_no_stock(salon) -> None:  # noqa: ANN001
    database, config, _, orders = salon
    before = _balances(database)
    order = _open(database, config, orders)

    item = _add(orders, order.id, _product(database, "CAFE-EXP"))

    assert _consumed(database, item) == {}
    assert _movements(database, item) == []
    assert _balances(database) == before


def test_resending_the_same_item_does_not_write_off_twice(salon) -> None:  # noqa: ANN001
    """O Wi-Fi caiu e o celular reenviou: o item é um só, a baixa também."""
    database, config, _, orders = salon
    order = _open(database, config, orders)
    uuid = new_id()
    slice_id = _product(database, "FATIA-CHOC")

    item = _add(orders, order.id, slice_id, uuid=uuid)
    after_first = _balances(database)
    orders.add_item(
        order_id=order.id, client_uuid=EntityId(uuid), product_id=slice_id, quantity=Decimal("1"),
    )

    assert _balances(database) == after_first
    assert len(_movements(database, item)) == 5


def test_a_store_that_blocks_negative_stock_refuses_at_the_table(tmp_path: Path) -> None:
    """A mesma política do balcão, e a recusa que o app do garçom já sabe mostrar."""
    database, config, _, orders = _salon(tmp_path, block=True)
    database.connection.execute("UPDATE inventory_items SET balance_mg = 0")
    database.connection.commit()
    order = _open(database, config, orders)

    with pytest.raises(ProductNotSellableError, match="Estoque insuficiente"):
        _add(orders, order.id, _product(database, "FATIA-CHOC"))

    # Nada pela metade: nem item, nem ticket, nem movimento.
    assert orders.list_items(order.id) == []
    assert database.query_one("SELECT COUNT(*) AS n FROM stock_movements")["n"] == 0
    assert database.query_one("SELECT COUNT(*) AS n FROM kds_tickets")["n"] == 0


def test_a_store_that_only_warns_still_launches(tmp_path: Path) -> None:
    """O padrão do food service: avisa e não trava a fila da cozinha."""
    database, config, _, orders = _salon(tmp_path)
    database.connection.execute("UPDATE inventory_items SET balance_mg = 0")
    database.connection.commit()
    order = _open(database, config, orders)

    item = _add(orders, order.id, _product(database, "FATIA-CHOC"))

    assert len(_movements(database, item)) == 5


# --------------------------------------------------------------------------- #
# O estorno
# --------------------------------------------------------------------------- #


def test_canceling_the_order_restores_the_stock(salon) -> None:  # noqa: ANN001
    database, config, _, orders = salon
    before = _balances(database)
    order = _open(database, config, orders)
    slice_id = _product(database, "FATIA-CHOC")
    items = [_add(orders, order.id, slice_id), _add(orders, order.id, slice_id, quantity="3")]
    _add(orders, order.id, _product(database, "CAFE-EXP"))
    assert _balances(database) != before, "lançar baixou insumo"

    orders.cancel_order(
        order_id=order.id, authorizer_id=EntityId(DEMO_MANAGER_ID),
        authorizer_name=DEMO_MANAGER_NAME, reason="Mesa aberta por engano",
    )

    assert _balances(database) == before
    for item in items:
        consumed = _consumed(database, item)
        reversals = [m for m in _movements(database, item) if m[0] == "adjustment"]
        assert sorted(reversals) == sorted(
            ("adjustment", "order_item_cancel", mg) for mg in consumed.values()
        )


def test_an_item_moved_to_another_order_is_restored_there(salon) -> None:  # noqa: ANN001
    """O consumo acompanha o item: mover de comanda não o perde no estorno."""
    database, config, _, orders = salon
    before = _balances(database)
    source = _open(database, config, orders, "Mesa 3")
    target = _open(database, config, orders, "Mesa 4")
    item = _add(orders, source.id, _product(database, "FATIA-CHOC"))
    _add(orders, source.id, _product(database, "CAFE-EXP"))
    assert _consumed(database, item)

    orders.move_items(
        source_order_id=source.id, target_order_id=target.id, item_ids=[item],
        operator_id=EntityId(DEMO_OPERATOR_ID), operator_name="Ana Caixa",
    )
    orders.cancel_order(
        order_id=target.id, authorizer_id=EntityId(DEMO_MANAGER_ID),
        authorizer_name=DEMO_MANAGER_NAME, reason="Cliente foi embora",
    )

    assert _balances(database) == before


def test_a_paid_order_keeps_its_write_off(salon) -> None:  # noqa: ANN001
    """Receber não mexe no estoque: a baixa já aconteceu no lançamento."""
    database, config, _, orders = salon
    order = _open(database, config, orders)
    item = _add(orders, order.id, _product(database, "FATIA-CHOC"))
    after_launch = _balances(database)

    total = int(orders.get_order(order.id).total_cents)
    orders.settle(
        order_id=order.id, payments=(Payment(PaymentMethod.PIX, Cents(total)),),
        operator_id=EntityId(DEMO_OPERATOR_ID), operator_name="Ana Caixa",
    )

    assert _balances(database) == after_launch
    assert [m[0] for m in _movements(database, item)] == ["sale"] * 5


def test_a_remote_cancel_of_a_table_item_restores_the_stock(salon) -> None:  # noqa: ANN001
    """O painel cancela o item da mesa; o caixa aceita; o insumo volta."""
    database, config, hub, orders = salon
    before = _balances(database)
    order = _open(database, config, orders)
    item = _add(orders, order.id, _product(database, "FATIA-CHOC"))
    _add(orders, order.id, _product(database, "CAFE-EXP"))
    assert _balances(database) != before, "lançar baixou insumo"

    payload = {"order_id": str(order.id), "order_item_id": str(item), "reason": "Prato errado"}
    command_uuid, issued_at = new_id(), iso(utc_now())
    command = RemoteCommand(
        command_uuid=command_uuid, tenant_id=TENANT, store_id=STORE, device_id=DEVICE,
        kind=CommandKind.CANCEL_ITEM, payload=payload,
        issued_by_user_id=DEMO_MANAGER_ID, issued_by_name=DEMO_MANAGER_NAME, issued_at=issued_at,
        signature=sign_command(
            secret=SECRET, command_uuid=command_uuid, device_id=DEVICE,
            kind=CommandKind.CANCEL_ITEM.value, payload=payload, issued_at=issued_at,
        ),
    )
    service = RemoteCommandService(database, config, hub=hub)
    InboxRepository(database).accept(command)
    # O prato já está na fila da cozinha: o cancelamento espera o aceite do caixa.
    assert service.apply_pending().awaiting == 1
    service.confirm(command_uuid, login=DEMO_OPERATOR_LOGIN, pin=DEMO_OPERATOR_PIN)

    assert _balances(database) == before
    assert sorted(m for m in _movements(database, item) if m[0] == "adjustment") == sorted(
        ("adjustment", "order_item_cancel", mg) for mg in _consumed(database, item).values()
    )
