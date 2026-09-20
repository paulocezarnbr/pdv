from __future__ import annotations

from pathlib import Path

import pytest

from pdv.config import AppConfig
from pdv.data.database import Database, SCHEMA_VERSION
from pdv.domain.models import Cents, EntityId, new_id
from pdv.services.cashback import CashbackService
from pdv.services.credit_account import CreditAccountError, CreditAccountService


@pytest.fixture()
def account(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path); database.migrate()
    customer = CashbackService(database, config).create_customer(name="Lia", phone="1199")
    service = CreditAccountService(database, config)
    operator, manager = EntityId(new_id()), EntityId(new_id())
    service.configure(customer_id=customer, limit_cents=Cents(5000), due_days=30,
                      actor_user_id=operator, authorizer_user_id=manager)
    yield database, service, customer, operator
    database.close()


def test_schema_is_current(account) -> None:  # noqa: ANN001
    database, *_ = account
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_charge_reduces_available_limit(account) -> None:  # noqa: ANN001
    database, service, customer, operator = account
    with database.transaction() as connection:
        position = service.charge_in(connection, customer_id=customer,
                                     order_id=EntityId(new_id()),
                                     amount_cents=Cents(1200), actor_user_id=operator)
    assert position.outstanding_cents == 1200
    assert position.available_cents == 3800


def test_limit_is_enforced_atomically(account) -> None:  # noqa: ANN001
    database, service, customer, operator = account
    with pytest.raises(CreditAccountError, match="insuficiente"):
        with database.transaction() as connection:
            service.charge_in(connection, customer_id=customer, order_id=EntityId(new_id()),
                              amount_cents=Cents(5001), actor_user_id=operator)
    assert database.query_one(
        "SELECT count(*) n FROM credit_account_ledger WHERE entry_type='charge'"
    )["n"] == 0


def test_payment_allocates_oldest_debt_without_rewriting_charge(account) -> None:  # noqa: ANN001
    database, service, customer, operator = account
    for amount in (1000, 2000):
        with database.transaction() as connection:
            service.charge_in(connection, customer_id=customer, order_id=EntityId(new_id()),
                              amount_cents=Cents(amount), actor_user_id=operator)
    position = service.pay(customer_id=customer, amount_cents=Cents(1500),
                           actor_user_id=operator)
    assert position.outstanding_cents == 1500
    assert database.query_one(
        "SELECT count(*) n FROM credit_account_ledger WHERE entry_type='charge'"
    )["n"] == 2
    assert database.query_one(
        "SELECT count(*) n FROM credit_account_ledger WHERE entry_type='payment'"
    )["n"] == 2


def test_same_order_is_not_charged_twice(account) -> None:  # noqa: ANN001
    database, service, customer, operator = account
    order = EntityId(new_id())
    for _ in range(2):
        with database.transaction() as connection:
            service.charge_in(connection, customer_id=customer, order_id=order,
                              amount_cents=Cents(500), actor_user_id=operator)
    assert service.position(customer).outstanding_cents == 500


def test_configuration_and_ledger_are_queued(account) -> None:  # noqa: ANN001
    database, service, customer, operator = account
    with database.transaction() as connection:
        service.charge_in(connection, customer_id=customer, order_id=EntityId(new_id()),
                          amount_cents=Cents(100), actor_user_id=operator)
    tables = [row[0] for row in database.connection.execute(
        "SELECT entity_table FROM sync_outbox ORDER BY seq"
    )]
    assert "customer_credit_accounts" in tables
    assert "credit_account_ledger" in tables
