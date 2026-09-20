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
from pdv.services.cashback import CashbackError, CashbackService


@pytest.fixture()
def cashback(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path)
    database.migrate()
    service = CashbackService(database, config)
    customer = service.create_customer(name="Cliente Teste", phone="11999999999")
    service.configure(percent=Decimal("5"), max_per_sale_cents=Cents(300), validity_days=30)
    yield database, service, customer
    database.close()


def test_fresh_database_contains_cashback_schema(cashback) -> None:  # noqa: ANN001
    database, _, _ = cashback
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert database.connection.execute("SELECT count(*) FROM cashback_rules").fetchone()[0] == 1


def test_earn_rounds_once_and_obeys_cap(cashback) -> None:  # noqa: ANN001
    _, service, customer = cashback
    actor = EntityId(new_id())
    first = service.earn(customer_id=customer, order_id=EntityId(new_id()),
                         eligible_cents=Cents(1999), actor_user_id=actor)
    capped = service.earn(customer_id=customer, order_id=EntityId(new_id()),
                          eligible_cents=Cents(10000), actor_user_id=actor)
    assert first is not None and first.amount_cents == 100
    assert capped is not None and capped.amount_cents == 300
    assert service.balance(customer) == 400


def test_same_order_is_credited_only_once(cashback) -> None:  # noqa: ANN001
    database, service, customer = cashback
    order = EntityId(new_id())
    actor = EntityId(new_id())
    a = service.earn(customer_id=customer, order_id=order,
                     eligible_cents=Cents(1000), actor_user_id=actor)
    b = service.earn(customer_id=customer, order_id=order,
                     eligible_cents=Cents(99999), actor_user_id=actor)
    assert a == b
    assert database.connection.execute(
        "SELECT count(*) FROM cashback_ledger WHERE order_id=?", (order,)
    ).fetchone()[0] == 1


def test_redeem_uses_fifo_lots_and_never_updates_credit(cashback) -> None:  # noqa: ANN001
    database, service, customer = cashback
    actor = EntityId(new_id())
    service.earn(customer_id=customer, order_id=EntityId(new_id()),
                 eligible_cents=Cents(2000), actor_user_id=actor)
    service.earn(customer_id=customer, order_id=EntityId(new_id()),
                 eligible_cents=Cents(4000), actor_user_id=actor)
    service.redeem(customer_id=customer, order_id=EntityId(new_id()),
                   amount_cents=Cents(250), actor_user_id=actor)
    assert service.balance(customer) == 50
    assert database.connection.execute(
        "SELECT count(*) FROM cashback_ledger WHERE entry_type='credit'"
    ).fetchone()[0] == 2
    assert database.connection.execute(
        "SELECT count(*) FROM cashback_ledger WHERE entry_type='debit'"
    ).fetchone()[0] == 2


def test_insufficient_balance_rolls_back_every_debit(cashback) -> None:  # noqa: ANN001
    database, service, customer = cashback
    actor = EntityId(new_id())
    service.earn(customer_id=customer, order_id=EntityId(new_id()),
                 eligible_cents=Cents(1000), actor_user_id=actor)
    with pytest.raises(CashbackError, match="insuficiente"):
        service.redeem(customer_id=customer, order_id=EntityId(new_id()),
                       amount_cents=Cents(51), actor_user_id=actor)
    assert database.connection.execute(
        "SELECT count(*) FROM cashback_ledger WHERE entry_type='debit'"
    ).fetchone()[0] == 0


def test_customer_and_ledger_are_queued_for_sync(cashback) -> None:  # noqa: ANN001
    database, service, customer = cashback
    service.earn(customer_id=customer, order_id=EntityId(new_id()),
                 eligible_cents=Cents(1000), actor_user_id=EntityId(new_id()))
    tables = [row[0] for row in database.connection.execute(
        "SELECT entity_table FROM sync_outbox ORDER BY seq"
    )]
    assert tables == ["customers", "cashback_ledger"]


def test_checkout_credits_cashback_atomically_and_prints_it(tmp_path: Path) -> None:
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path)
    database.migrate()
    seed_demo_data(database, config)
    cashback = CashbackService(database, config)
    cashback.configure(percent=Decimal("10"), max_per_sale_cents=Cents(500), validity_days=30)
    customer = cashback.create_customer(name="Lia", phone="(11) 99999-0000")
    coffee = next(product for product in ProductRepository(database.connection).list_active(
        EntityId(config.tenant_id)
    ) if product.sku == "CAFE-EXP")
    checkout = CheckoutService(database, config)
    checkout.register_unit_item(product=coffee, quantity=Decimal("1"),
                                operator_id=EntityId(DEMO_OPERATOR_ID))
    receipt = checkout.finalize_sale(
        payments=(Payment(PaymentMethod.PIX, Cents(700)),),
        operator_id=EntityId(DEMO_OPERATOR_ID), operator_name="Ana Caixa",
        customer_id=customer, customer_name="Lia",
    )
    text = receipt.decode("cp850", errors="replace")
    assert "Cliente" in text and "Lia" in text
    assert "Cashback creditado" in text and "0,70" in text
    assert cashback.balance(customer) == 70
    assert database.query_one(
        "SELECT event_type FROM audit_ledger WHERE event_type='cashback_credited'"
    ) is not None
