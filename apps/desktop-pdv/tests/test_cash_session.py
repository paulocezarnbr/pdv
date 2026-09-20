from __future__ import annotations

from pathlib import Path
import json

import pytest

from pdv.config import AppConfig
from pdv.data.database import Database
from pdv.data.seed import DEMO_MANAGER_ID, DEMO_OPERATOR_ID, seed_demo_data
from pdv.domain.models import Cents, EntityId, iso, new_id, utc_now
from pdv.services.cash_session import CashSessionError, CashSessionService


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(tenant_id="tenant", store_id="store", device_id="device",
                       database_path=tmp_path / "pdv.db")
    database = Database(config.database_path)
    database.migrate(); seed_demo_data(database, config)
    return database, config, CashSessionService(database, config)


def _cash_payment(database: Database, config: AppConfig, amount: int, change: int = 0) -> None:
    now = iso(utc_now()); order_id = new_id()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO orders (id,tenant_id,store_id,device_id,local_number,channel,status,operator_id,opened_at,closed_at,subtotal_cents,total_cents,created_at,updated_at,client_uuid,origin_device_id) VALUES (?,?,?,?,1,'counter','paid',?,?,?,?,?,?,?,?,?)",
            (order_id, config.tenant_id, config.store_id, config.device_id,
             DEMO_OPERATOR_ID, now, now, amount-change, amount-change, now, now, new_id(), config.device_id),
        )
        connection.execute(
            "INSERT INTO payments (id,order_id,tenant_id,method,amount_cents,change_cents,created_at,client_uuid) VALUES (?,?,?,'cash',?,?,?,?)",
            (new_id(), order_id, config.tenant_id, amount, change, now, new_id()),
        )


def test_open_state_never_exposes_the_expected_amount(env) -> None:  # noqa: ANN001
    _database, _config, service = env
    opened = service.open(operator_id=EntityId(DEMO_OPERATOR_ID), opening_cents=Cents(10_000))
    assert opened.opening_cents == 10_000
    assert not hasattr(opened, "expected_cents")
    assert not hasattr(service.current(), "expected_cents")


def test_blind_close_counts_cash_and_change(env) -> None:  # noqa: ANN001
    database, config, service = env
    service.open(operator_id=EntityId(DEMO_OPERATOR_ID), opening_cents=Cents(10_000))
    _cash_payment(database, config, 5_000, 500)
    result = service.close(declared_cents=Cents(14_300), operator_id=EntityId(DEMO_OPERATOR_ID), authorizer_id=EntityId(DEMO_MANAGER_ID))
    assert result.expected_cents == 14_500
    assert result.difference_cents == -200
    row = database.connection.execute("SELECT * FROM cash_sessions WHERE id=?", (result.session_id,)).fetchone()
    assert row["declared_amount_cents"] == 14_300
    assert row["expected_amount_cents"] == 14_500
    assert row["difference_cents"] == -200


def test_close_is_atomic_with_audit_and_outbox(env) -> None:  # noqa: ANN001
    database, _config, service = env
    opened = service.open(operator_id=EntityId(DEMO_OPERATOR_ID), opening_cents=Cents(0))
    service.close(declared_cents=Cents(0), operator_id=EntityId(DEMO_OPERATOR_ID))
    assert database.connection.execute("SELECT count(*) FROM audit_ledger WHERE event_type='session_closed'").fetchone()[0] == 1
    payload = database.connection.execute("SELECT payload_json FROM sync_outbox WHERE entity_table='cash_sessions' AND entity_id=?", (opened.id,)).fetchone()
    assert payload is not None
    assert json.loads(payload[0])["blind_close"] is True


def test_second_operator_cannot_take_an_open_drawer(env) -> None:  # noqa: ANN001
    _database, _config, service = env
    service.open(operator_id=EntityId(DEMO_OPERATOR_ID), opening_cents=Cents(0))
    with pytest.raises(CashSessionError, match="outro operador"):
        service.open(operator_id=EntityId(DEMO_MANAGER_ID), opening_cents=Cents(0))


def test_negative_values_are_rejected(env) -> None:  # noqa: ANN001
    _database, _config, service = env
    with pytest.raises(CashSessionError):
        service.open(operator_id=EntityId(DEMO_OPERATOR_ID), opening_cents=Cents(-1))
