"""Dividir e juntar conta — operação de caixa.

O que cada teste cobra é uma das quatro garantias de `edge/orders.py`: o
dinheiro não some nem aparece, o item leva a cozinha junto, a mesa continua com
uma comanda aberta só, e quem mexeu fica no ledger.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, StockConfig
from pdv.data.database import Database
from pdv.data.seed import DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.errors import InsufficientPaymentError
from pdv.domain.models import Cents, EntityId, Payment, PaymentMethod, new_id
from pdv.edge.orders import OrderClosedError, TableOrder, TableOrderService
from pdv.edge.tables import TableService

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
DEVICE = "33333333-3333-3333-3333-333333333333"
CASHIER = EntityId("caixa-ana")


@pytest.fixture()
def salon(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT, store_id=STORE, device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
        stock=StockConfig(),
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    return database, TableService(database, config), TableOrderService(database, config)


def _cafe(database: Database) -> EntityId:
    return EntityId(str(database.query_one("SELECT id FROM products WHERE sku = 'CAFE-EXP'")["id"]))


def _table(salon, label: str, items: int) -> TableOrder:  # noqa: ANN001
    database, tables, orders = salon
    order = orders.open_order(
        client_uuid=EntityId(new_id()), operator_id=EntityId(DEMO_OPERATOR_ID),
        table_id=tables.find_by_label(label).id, origin_device_id=EntityId(new_id()),
    )
    for _ in range(items):
        orders.add_item(order_id=order.id, client_uuid=EntityId(new_id()),
                        product_id=_cafe(database), quantity=Decimal(1))
    return orders.get_order(order.id)


def _items(database: Database, order: TableOrder) -> list[EntityId]:
    return [
        EntityId(str(r["id"])) for r in database.query_all(
            "SELECT id FROM order_items WHERE order_id = ? AND canceled_at IS NULL "
            "ORDER BY created_at", (order.id,),
        )
    ]


def _audit(database: Database, event: str) -> list[dict]:
    return [
        json.loads(r["payload_json"]) for r in database.query_all(
            "SELECT payload_json FROM audit_ledger WHERE event_type = ?", (event,)
        )
    ]


# --------------------------------------------------------------------------- #
# Mover itens
# --------------------------------------------------------------------------- #


def test_moving_items_keeps_the_money(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 3)
    five = _table(salon, "Mesa 5", 1)
    before = int(four.total_cents) + int(five.total_cents)

    four, five = orders.move_items(
        source_order_id=four.id, target_order_id=five.id,
        item_ids=_items(database, four)[:2], operator_id=CASHIER, operator_name="Ana",
    )

    assert (four.item_count, five.item_count) == (1, 3)
    assert int(four.total_cents) + int(five.total_cents) == before
    assert int(five.total_cents) == 3 * int(four.total_cents)


def test_the_kitchen_ticket_follows_the_item(salon) -> None:  # noqa: ANN001
    """Sem isto o corredor entrega o prato na mesa que não o pediu."""
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 1)
    five = _table(salon, "Mesa 5", 1)
    item = _items(database, four)[0]

    orders.move_items(source_order_id=four.id, target_order_id=five.id, item_ids=[item],
                      operator_id=CASHIER, operator_name="Ana")

    ticket = database.query_one("SELECT order_id FROM kds_tickets WHERE order_item_id = ?", (item,))
    assert ticket["order_id"] == five.id


def test_moving_is_in_the_ledger_with_who_did_it(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    five = _table(salon, "Mesa 5", 1)

    orders.move_items(source_order_id=four.id, target_order_id=five.id,
                      item_ids=_items(database, four)[:1], operator_id=CASHIER, operator_name="Ana")

    [entry] = _audit(database, "items_transferred")
    assert (entry["from_table"], entry["to_table"], entry["operator_name"]) == ("Mesa 4", "Mesa 5", "Ana")
    assert entry["total_cents"] == int(four.total_cents) // 2


def test_an_item_of_another_tab_cannot_be_moved(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 1)
    five = _table(salon, "Mesa 5", 1)

    with pytest.raises(OrderClosedError, match="não estão vivos"):
        orders.move_items(source_order_id=four.id, target_order_id=five.id,
                          item_ids=_items(database, five), operator_id=CASHIER, operator_name="Ana")


def test_a_canceled_item_stays_where_it_was(salon) -> None:  # noqa: ANN001
    """Item cancelado é histórico da comanda em que foi cancelado."""
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    five = _table(salon, "Mesa 5", 1)
    gone = _items(database, four)[0]
    database.connection.execute("UPDATE order_items SET canceled_at = 'x' WHERE id = ?", (gone,))
    database.connection.commit()

    with pytest.raises(OrderClosedError):
        orders.move_items(source_order_id=four.id, target_order_id=five.id, item_ids=[gone],
                          operator_id=CASHIER, operator_name="Ana")


def test_nothing_moves_into_a_closed_tab(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 1)
    five = _table(salon, "Mesa 5", 1)
    orders.settle(order_id=five.id, payments=(Payment(PaymentMethod.CASH, five.total_cents),),
                  operator_id=CASHIER, operator_name="Ana")

    with pytest.raises(OrderClosedError):
        orders.move_items(source_order_id=four.id, target_order_id=five.id,
                          item_ids=_items(database, four), operator_id=CASHIER, operator_name="Ana")


# --------------------------------------------------------------------------- #
# Juntar
# --------------------------------------------------------------------------- #


def test_merging_frees_the_table_and_keeps_the_bill(salon) -> None:  # noqa: ANN001
    database, tables, orders = salon
    four = _table(salon, "Mesa 4", 2)
    five = _table(salon, "Mesa 5", 1)
    orders.request_bill(four.id)

    merged = orders.merge_orders(source_order_id=four.id, target_order_id=five.id,
                                 operator_id=CASHIER, operator_name="Ana")

    assert merged.item_count == 3
    assert int(merged.total_cents) == int(four.total_cents) + int(five.total_cents)
    closed = orders.get_order(four.id)
    assert (closed.status, int(closed.total_cents), closed.bill_requested) == ("canceled", 0, False)
    assert next(t for t in tables.list_tables() if t.label == "Mesa 4").status == "free"


def test_merging_is_not_a_cancellation_in_the_ledger(salon) -> None:  # noqa: ANN001
    """Junção e cancelamento são alarmes diferentes para o dono."""
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 1)
    five = _table(salon, "Mesa 5", 1)

    orders.merge_orders(source_order_id=four.id, target_order_id=five.id,
                        operator_id=CASHIER, operator_name="Ana")

    assert _audit(database, "item_canceled") == []
    [entry] = _audit(database, "order_merged")
    assert (entry["from_table"], entry["to_table"], entry["items"]) == ("Mesa 4", "Mesa 5", 1)


def test_a_tab_cannot_merge_into_itself(salon) -> None:  # noqa: ANN001
    _, _, orders = salon
    four = _table(salon, "Mesa 4", 1)

    with pytest.raises(OrderClosedError, match="mesma comanda"):
        orders.merge_orders(source_order_id=four.id, target_order_id=four.id,
                            operator_id=CASHIER, operator_name="Ana")


# --------------------------------------------------------------------------- #
# Pagar parte
# --------------------------------------------------------------------------- #


def test_paying_part_leaves_the_rest_open(salon) -> None:  # noqa: ANN001
    database, tables, orders = salon
    four = _table(salon, "Mesa 4", 3)
    unit = int(four.total_cents) // 3
    mine = _items(database, four)[:1]

    part = orders.settle_items(order_id=four.id, item_ids=mine,
                               payments=(Payment(PaymentMethod.PIX, Cents(unit)),),
                               operator_id=CASHIER, operator_name="Ana")

    assert part.order.status == "paid"
    assert (part.order.item_count, int(part.order.total_cents)) == (1, unit)
    assert part.order.table_label == "Mesa 4"
    rest = orders.get_order(four.id)
    assert (rest.status, rest.item_count, int(rest.total_cents)) == ("open", 2, 2 * unit)
    open_on_table = database.query_one(
        "SELECT COUNT(*) AS n FROM orders WHERE table_id = ? AND status = 'open'", (four.table_id,)
    )["n"]
    assert open_on_table == 1, "a mesa nunca fica com duas comandas abertas"
    assert part.receipt, "cada pagante leva o próprio cupom"


def test_the_paid_part_stays_with_the_waiter(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)

    part = orders.settle_items(order_id=four.id, item_ids=_items(database, four)[:1],
                               payments=(Payment(PaymentMethod.CASH, Cents(int(four.total_cents) // 2)),),
                               operator_id=CASHIER, operator_name="Ana")

    assert part.order.operator_id == EntityId(DEMO_OPERATOR_ID)
    [closed] = [e for e in _audit(database, "sale_closed") if e["order_id"] == part.order.id]
    assert closed["split_from_local_number"] == four.local_number


def test_paying_the_same_items_twice_is_refused(salon) -> None:  # noqa: ANN001
    """O clique duplo no caixa não cobra duas vezes."""
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    mine = _items(database, four)[:1]
    pay = (Payment(PaymentMethod.CASH, Cents(int(four.total_cents) // 2)),)
    orders.settle_items(order_id=four.id, item_ids=mine, payments=pay,
                        operator_id=CASHIER, operator_name="Ana")

    with pytest.raises(OrderClosedError):
        orders.settle_items(order_id=four.id, item_ids=mine, payments=pay,
                            operator_id=CASHIER, operator_name="Ana")


def test_the_part_must_be_paid_in_full(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)

    with pytest.raises(InsufficientPaymentError):
        orders.settle_items(order_id=four.id, item_ids=_items(database, four)[:1],
                            payments=(Payment(PaymentMethod.CASH, Cents(1)),),
                            operator_id=CASHIER, operator_name="Ana")
    assert orders.get_order(four.id).item_count == 2, "recusa não move nada"


def test_the_tip_of_a_part_is_outside_its_total(salon) -> None:  # noqa: ANN001
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    half = int(four.total_cents) // 2

    part = orders.settle_items(order_id=four.id, item_ids=_items(database, four)[:1],
                               payments=(Payment(PaymentMethod.DEBIT, Cents(half + 100)),),
                               operator_id=CASHIER, operator_name="Ana", tip_cents=Cents(100))

    assert (int(part.order.total_cents), int(part.tip_cents)) == (half, 100)


def test_choosing_every_item_is_the_whole_bill(salon) -> None:  # noqa: ANN001
    """Sem comanda nova: é o recebimento de sempre, e a mesa se libera."""
    database, tables, orders = salon
    four = _table(salon, "Mesa 4", 2)

    paid = orders.settle_items(order_id=four.id, item_ids=_items(database, four),
                               payments=(Payment(PaymentMethod.CASH, four.total_cents),),
                               operator_id=CASHIER, operator_name="Ana")

    assert paid.order.id == four.id
    assert next(t for t in tables.list_tables() if t.label == "Mesa 4").status == "free"


def test_every_split_leaves_a_clean_trail_for_the_cloud(salon) -> None:  # noqa: ANN001
    """Item antes da conta, e a comanda nova antes do item que entra nela.

    Na nuvem, item só muda para comanda aberta que já chegou; fora dessa ordem
    o `update` iria para a quarentena.
    """
    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    database.connection.execute("DELETE FROM sync_outbox")
    database.connection.commit()

    part = orders.settle_items(order_id=four.id, item_ids=_items(database, four)[:1],
                               payments=(Payment(PaymentMethod.CASH, Cents(int(four.total_cents) // 2)),),
                               operator_id=CASHIER, operator_name="Ana")

    trail = [
        (r["entity_table"], r["operation"], json.loads(r["payload_json"]))
        for r in database.query_all(
            "SELECT entity_table, operation, payload_json FROM sync_outbox ORDER BY seq")
    ]
    kinds = [(t, op) for t, op, _ in trail if t != "audit_ledger"]
    child = part.order.id
    assert kinds[0] == ("orders", "insert"), "a comanda nova sobe primeiro"
    assert kinds[1] == ("order_items", "update")
    assert trail[-1][2].get("status") == "paid" or any(
        p.get("status") == "paid" and p.get("id") == child for _, _, p in trail)
    paid_at = next(i for i, (_, _, p) in enumerate(trail) if p.get("status") == "paid")
    moved_at = next(i for i, (t, _, _) in enumerate(trail) if t == "order_items")
    assert moved_at < paid_at, "o item muda antes da comanda fechar"


# --------------------------------------------------------------------------- #
# A tela do caixa
# --------------------------------------------------------------------------- #


def test_the_picker_shows_the_total_of_what_is_marked(qtbot, salon) -> None:  # noqa: ANN001
    """O cliente confere o valor da parte dele antes de pagar, não depois."""
    from pdv.ui.tables_dialog import ItemPickerDialog

    database, _, orders = salon
    four = _table(salon, "Mesa 4", 3)
    items = orders.list_items(four.id)
    unit = int(items[0]["total_cents"])
    dialog = ItemPickerDialog(items, title="t", confirm="ok")
    qtbot.addWidget(dialog)

    assert not dialog._ok.isEnabled(), "nada marcado, nada a confirmar"  # noqa: SLF001
    dialog.check(0)
    dialog.check(2)

    assert dialog.total_cents == 2 * unit
    assert dialog.selected == [EntityId(str(items[0]["id"])), EntityId(str(items[2]["id"]))]


def test_a_canceled_item_is_not_offered(qtbot, salon) -> None:  # noqa: ANN001
    from pdv.ui.tables_dialog import ItemPickerDialog

    database, _, orders = salon
    four = _table(salon, "Mesa 4", 2)
    items = orders.list_items(four.id)
    items[0] = {**items[0], "canceled": True}

    dialog = ItemPickerDialog(items, title="t", confirm="ok")
    qtbot.addWidget(dialog)

    assert dialog._table.rowCount() == 1  # noqa: SLF001


def test_the_cashier_screen_offers_split_move_and_merge(qtbot, salon, tmp_path) -> None:  # noqa: ANN001
    from pdv.config import AppConfig, PrinterConfig, StockConfig
    from pdv.services.authorization import Identity
    from pdv.ui.tables_dialog import TablesDialog
    from PySide6.QtWidgets import QPushButton

    database, _, _ = salon
    config = AppConfig(tenant_id=TENANT, store_id=STORE, device_id=DEVICE,
                       database_path=tmp_path / "pdv.db",
                       printer=PrinterConfig(backend="file", output_dir=tmp_path / "out"),
                       stock=StockConfig())
    operator = Identity(id=CASHIER, name="Ana", login="ana", role="cashier",
                        can_authorize=False, max_discount_percent=Decimal(0))
    dialog = TablesDialog(database, config, operator=operator)
    qtbot.addWidget(dialog)

    labels = {b.text() for b in dialog.findChildren(QPushButton)}
    assert {"Pagar parte", "Mover itens", "Juntar comandas"} <= labels
