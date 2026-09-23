from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pdv.config import AppConfig
from pdv.data.database import SCHEMA_VERSION, Database
from pdv.domain.models import EntityId, iso, new_id, utc_now
from pdv.fiscal import FiscalError, FiscalNotRequired, FiscalService


@pytest.fixture()
def env(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(tenant_id="tenant", store_id="store", device_id="device",
                       database_path=tmp_path / "pdv.db")
    database = Database(config.database_path)
    database.migrate()
    return database, config, FiscalService(database, config)


def _paid_order(
    database: Database, config: AppConfig, *, total_cents: int = 1000,
    subtotal_cents: int = 1000,
) -> EntityId:
    order_id, now = new_id(), iso(utc_now())
    with database.transaction() as connection:
        local_number = database.next_counter(connection, "order_local_number")
        connection.execute(
            "INSERT INTO orders "
            "(id,tenant_id,store_id,device_id,local_number,channel,status,operator_id,"
            "subtotal_cents,total_cents,opened_at,closed_at,created_at,updated_at,"
            "origin_device_id,client_uuid) VALUES (?,?,?,?,?,'counter','paid','operator',"
            "?,?,?,?,?,?,?,?)",
            (order_id, config.tenant_id, config.store_id, config.device_id, local_number,
             subtotal_cents, total_cents, now, now, now, now, config.device_id, new_id()),
        )
    return order_id


def test_fresh_database_reaches_fiscal_schema(env) -> None:  # noqa: ANN001
    database, _config, _service = env
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert database.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fiscal_documents'"
    ).fetchone() is not None


def test_version_12_database_is_upgraded_without_touching_sales(env) -> None:  # noqa: ANN001
    database, config, _service = env
    order_id = _paid_order(database, config)
    database.connection.executescript(
        "DROP TABLE fiscal_events; DROP TABLE fiscal_documents; DROP TABLE fiscal_series; "
        "PRAGMA user_version = 12;"
    )
    database.close()
    upgraded = Database(config.database_path)
    upgraded.migrate()
    assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 13
    assert upgraded.connection.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone() is not None
    assert upgraded.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fiscal_documents'"
    ).fetchone() is not None


def test_offline_reservation_is_idempotent_and_marks_contingency(env) -> None:  # noqa: ANN001
    database, config, service = env
    order_id = _paid_order(database, config)
    service.configure_series(series=101)
    first = service.reserve(order_id=order_id, online=False, contingency_reason="internet indisponível")
    repeated = service.reserve(order_id=order_id, online=False)
    assert first == repeated
    assert first.series == 101 and first.number == 1
    assert first.status == "contingency_pending"
    assert first.emission_type == "offline_contingency"
    assert first.contingency_reason == "internet indisponível"
    assert database.connection.execute("SELECT next_number FROM fiscal_series").fetchone()[0] == 2
    assert database.connection.execute("SELECT count(*) FROM fiscal_events").fetchone()[0] == 1


def test_concurrent_reservations_never_duplicate_a_number(env) -> None:  # noqa: ANN001
    database, config, service = env
    service.configure_series(series=102)
    order_ids = [_paid_order(database, config) for _ in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        documents = list(pool.map(
            lambda order_id: service.reserve(order_id=order_id, online=True), order_ids,
        ))
    assert sorted(document.number for document in documents) == list(range(1, 17))
    assert len({(document.series, document.number) for document in documents}) == 16


def test_same_order_racing_is_still_one_document(env) -> None:  # noqa: ANN001
    database, config, service = env
    service.configure_series(series=103)
    order_id = _paid_order(database, config)
    with ThreadPoolExecutor(max_workers=6) as pool:
        documents = list(pool.map(
            lambda _attempt: service.reserve(order_id=order_id, online=False), range(12),
        ))
    assert {document.id for document in documents} == {documents[0].id}
    assert database.connection.execute("SELECT count(*) FROM fiscal_documents").fetchone()[0] == 1
    assert database.connection.execute("SELECT next_number FROM fiscal_series").fetchone()[0] == 2


def test_terminal_cannot_issue_another_devices_order(env) -> None:  # noqa: ANN001
    database, config, service = env
    order_id = _paid_order(database, config)
    database.connection.execute("UPDATE orders SET device_id='other-device' WHERE id=?", (order_id,))
    service.configure_series(series=104)
    with pytest.raises(FiscalError, match="outro terminal"):
        service.reserve(order_id=order_id, online=True)


def test_series_cannot_change_after_first_document(env) -> None:  # noqa: ANN001
    database, config, service = env
    order_id = _paid_order(database, config)
    service.configure_series(series=105)
    service.reserve(order_id=order_id, online=True)
    with pytest.raises(FiscalError, match="não pode ser trocada"):
        service.configure_series(series=106)


def test_two_devices_cannot_share_the_same_series(env) -> None:  # noqa: ANN001
    database, config, service = env
    service.configure_series(series=108)
    second_config = AppConfig(tenant_id=config.tenant_id, store_id=config.store_id,
                              device_id="device-2", database_path=config.database_path)
    with pytest.raises(FiscalError, match="outro terminal"):
        FiscalService(database, second_config).configure_series(series=108)


def test_fiscal_history_cannot_be_deleted_or_rewritten(env) -> None:  # noqa: ANN001
    database, config, service = env
    order_id = _paid_order(database, config)
    service.configure_series(series=107)
    document = service.reserve(order_id=order_id, online=True)
    with pytest.raises(Exception, match="immutable"):
        database.connection.execute("DELETE FROM fiscal_documents WHERE id=?", (document.id,))
    with pytest.raises(Exception, match="immutable"):
        database.connection.execute("UPDATE fiscal_events SET event_type='forged'")


# --------------------------------------------------------------------------- #
# Venda com total zero
# --------------------------------------------------------------------------- #


def _next_number(database: Database) -> int:
    row = database.connection.execute("SELECT next_number FROM fiscal_series").fetchone()
    return int(row["next_number"])


def test_a_hundred_percent_discount_does_not_consume_a_contingency_number(env) -> None:  # noqa: ANN001
    """Desconto de 100%: a venda fecha, mas não há nota — nem de contingência.

    Uma NFC-e de R$ 0,00 seria rejeitada depois de consumir o número, e o
    buraco na série exigiria inutilização formal na SEFAZ.
    """
    database, config, service = env
    service.configure_series(series=101)
    courtesy = _paid_order(database, config, subtotal_cents=1000, total_cents=0)
    before = _next_number(database)

    with pytest.raises(FiscalNotRequired):
        service.reserve(order_id=courtesy, online=False)

    assert _next_number(database) == before
    assert database.connection.execute(
        "SELECT count(*) FROM fiscal_documents WHERE order_id=?", (courtesy,)
    ).fetchone()[0] == 0


def test_a_zero_priced_product_does_not_need_a_document(env) -> None:  # noqa: ANN001
    database, config, service = env
    free = _paid_order(database, config, subtotal_cents=0, total_cents=0)

    assert service.requires_document(free) is False


def test_a_partial_discount_still_needs_a_document(env) -> None:  # noqa: ANN001
    """A regra é sobre o total ZERO, não sobre ter desconto."""
    database, config, service = env
    almost = _paid_order(database, config, subtotal_cents=1000, total_cents=1)

    assert service.requires_document(almost) is True


def test_not_required_is_distinguishable_from_a_real_failure(env) -> None:  # noqa: ANN001
    """Quem chama trata `FiscalNotRequired` como desfecho, não como erro a repetir.

    Ela herda de `FiscalError` para não escapar de um `except FiscalError`
    antigo — mas é uma classe própria justamente para poder ser separada.
    """
    assert issubclass(FiscalNotRequired, FiscalError)
    assert FiscalNotRequired is not FiscalError


def test_a_negative_total_is_a_defect_not_a_courtesy(env) -> None:  # noqa: ANN001
    database, config, service = env
    broken = _paid_order(database, config, subtotal_cents=1000, total_cents=-50)

    with pytest.raises(FiscalError) as caught:
        service.requires_document(broken)
    assert not isinstance(caught.value, FiscalNotRequired)
