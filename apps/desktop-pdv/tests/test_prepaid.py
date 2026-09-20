from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig
from pdv.data.database import Database, SCHEMA_VERSION
from pdv.data.repositories import ProductRepository
from pdv.data.seed import DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import Cents, EntityId, Payment, PaymentMethod, new_id
from pdv.services.checkout import CheckoutService
from pdv.services.cashback import CashbackService
from pdv.services.prepaid import PrepaidError, PrepaidService


@pytest.fixture()
def wallet(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path)
    database.migrate()
    customer = CashbackService(database, config).create_customer(name="Lia", phone="1199")
    yield database, PrepaidService(database, config), customer
    database.close()


def test_schema_is_current(wallet) -> None:  # noqa: ANN001
    database, _, _ = wallet
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_deposit_and_debit_are_append_only(wallet) -> None:  # noqa: ANN001
    database, service, customer = wallet
    operator, manager = EntityId(new_id()), EntityId(new_id())
    assert service.deposit(customer_id=customer, amount_cents=Cents(5000),
                           actor_user_id=operator, authorizer_user_id=manager) == 5000
    order = EntityId(new_id())
    with database.transaction() as connection:
        assert service.redeem_in(connection, customer_id=customer, order_id=order,
                                 amount_cents=Cents(1250), actor_user_id=operator) == 3750
    assert service.balance(customer) == 3750
    rows = database.query_all("SELECT entry_type,amount_cents FROM prepaid_ledger ORDER BY created_at")
    assert [(row["entry_type"], row["amount_cents"]) for row in rows] == [
        ("deposit", 5000), ("debit", 1250)
    ]


def test_insufficient_balance_rolls_back(wallet) -> None:  # noqa: ANN001
    database, service, customer = wallet
    with pytest.raises(PrepaidError, match="insuficiente"):
        with database.transaction() as connection:
            service.redeem_in(connection, customer_id=customer, order_id=EntityId(new_id()),
                              amount_cents=Cents(1), actor_user_id=EntityId(new_id()))
    assert database.query_one("SELECT count(*) n FROM prepaid_ledger")["n"] == 0


def test_same_order_cannot_be_debited_twice(wallet) -> None:  # noqa: ANN001
    database, service, customer = wallet
    operator, manager, order = EntityId(new_id()), EntityId(new_id()), EntityId(new_id())
    service.deposit(customer_id=customer, amount_cents=Cents(1000),
                    actor_user_id=operator, authorizer_user_id=manager)
    for _ in range(2):
        with database.transaction() as connection:
            service.redeem_in(connection, customer_id=customer, order_id=order,
                              amount_cents=Cents(400), actor_user_id=operator)
    assert service.balance(customer) == 600
    assert database.query_one(
        "SELECT count(*) n FROM prepaid_ledger WHERE entry_type='debit'"
    )["n"] == 1


def test_every_entry_and_audit_is_queued(wallet) -> None:  # noqa: ANN001
    database, service, customer = wallet
    service.deposit(customer_id=customer, amount_cents=Cents(100),
                    actor_user_id=EntityId(new_id()), authorizer_user_id=EntityId(new_id()))
    tables = [row[0] for row in database.connection.execute(
        "SELECT entity_table FROM sync_outbox ORDER BY seq"
    )]
    assert tables == ["customers", "prepaid_ledger", "audit_ledger"]


def test_checkout_consumes_prepaid_in_same_transaction(tmp_path: Path) -> None:
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    customer = CashbackService(database, config).create_customer(name="Lia", phone="1199")
    prepaid = PrepaidService(database, config)
    prepaid.deposit(customer_id=customer, amount_cents=Cents(1000),
                    actor_user_id=EntityId(DEMO_OPERATOR_ID),
                    authorizer_user_id=EntityId(new_id()))
    coffee = next(product for product in ProductRepository(database.connection).list_active(
        EntityId(config.tenant_id)
    ) if product.sku == "CAFE-EXP")
    checkout = CheckoutService(database, config)
    checkout.register_unit_item(product=coffee, quantity=Decimal("1"),
                                operator_id=EntityId(DEMO_OPERATOR_ID))
    receipt = checkout.finalize_sale(
        payments=(Payment(PaymentMethod.PREPAID, Cents(700)),),
        operator_id=EntityId(DEMO_OPERATOR_ID), operator_name="Ana",
        customer_id=customer, customer_name="Lia",
    )
    assert prepaid.balance(customer) == 300
    assert "Saldo pre-pago" in receipt.decode("cp850", errors="replace")
    assert database.query_one("SELECT method FROM payments")["method"] == "prepaid"
