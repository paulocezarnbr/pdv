from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pdv.config import AppConfig
from pdv.data.database import Database, SCHEMA_VERSION
from pdv.data.seed import seed_demo_data
from pdv.domain.models import Cents, EntityId, new_id
from pdv.services.cashback import CashbackService
from pdv.services.discount_tiers import DiscountTierError, DiscountTierService


@pytest.fixture()
def tiers(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "pdv.db",
    )
    database = Database(config.database_path); database.migrate()
    customer = CashbackService(database, config).create_customer(name="Lia", phone="1199")
    service = DiscountTierService(database, config)
    actor = EntityId(new_id())
    yield database, service, customer, actor
    database.close()


def test_schema_is_current(tiers) -> None:  # noqa: ANN001
    database, *_ = tiers
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_configure_and_assign_resolves_customer_tier(tiers) -> None:  # noqa: ANN001
    _, service, customer, actor = tiers
    tier = service.configure(code="diamond", name="Diamante", percent=Decimal("12.5"),
                             priority=10, requires_manager=False, actor_user_id=actor)
    service.assign(customer_id=customer, tier_id=tier.id, actor_user_id=actor)
    resolved = service.for_customer(customer)
    assert resolved == tier
    assert resolved.discount_for(Cents(1999)) == 250


def test_unknown_level_is_rejected(tiers) -> None:  # noqa: ANN001
    _, service, _, actor = tiers
    with pytest.raises(DiscountTierError, match="desconhecido"):
        service.configure(code="inventado", name="X", percent=Decimal("1"),
                          priority=0, requires_manager=False, actor_user_id=actor)


def test_reconfigure_updates_instead_of_duplicating(tiers) -> None:  # noqa: ANN001
    database, service, _, actor = tiers
    a = service.configure(code="employee", name="Funcionário", percent=Decimal("10"),
                          priority=0, requires_manager=True, actor_user_id=actor)
    b = service.configure(code="employee", name="Funcionário", percent=Decimal("20"),
                          priority=0, requires_manager=True, actor_user_id=actor)
    assert a.id == b.id
    assert database.query_one("SELECT count(*) n FROM discount_tiers")["n"] == 1
    assert b.percent_basis_points == 2000


def test_owner_always_requires_manager_even_when_configuration_says_no(tiers) -> None:  # noqa: ANN001
    database, service, customer, actor = tiers
    owner = service.configure(code="owner", name="Dono", percent=Decimal("50"),
                              priority=100, requires_manager=False, actor_user_id=actor)
    assert owner.requires_manager is True
    service.assign(customer_id=customer, tier_id=owner.id, actor_user_id=actor)
    # Também protege um registro antigo/adulterado que tente desligar a trava.
    database.connection.execute(
        "UPDATE discount_tiers SET requires_manager=0 WHERE id=?", (owner.id,)
    )
    assert service.for_customer(customer).requires_manager is True


def test_configuration_and_assignment_are_synced_and_audited(tiers) -> None:  # noqa: ANN001
    database, service, customer, actor = tiers
    tier = service.configure(code="owner", name="Dono", percent=Decimal("100"),
                             priority=100, requires_manager=True, actor_user_id=actor)
    service.assign(customer_id=customer, tier_id=tier.id, actor_user_id=actor)
    tables = [row[0] for row in database.connection.execute(
        "SELECT entity_table FROM sync_outbox ORDER BY seq"
    )]
    assert "discount_tiers" in tables and "customer_discount_tiers" in tables
    events = {row[0] for row in database.connection.execute("SELECT event_type FROM audit_ledger")}
    assert {"discount_tier_configured", "discount_tier_assigned"} <= events


def test_demo_seed_creates_all_default_tiers_without_overwriting_them(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        tenant_id="11111111-1111-1111-1111-111111111111",
        store_id="22222222-2222-2222-2222-222222222222",
        device_id="33333333-3333-3333-3333-333333333333",
        database_path=tmp_path / "seed.db",
    )
    database = Database(config.database_path)
    database.migrate()
    try:
        seed_demo_data(database, config)
        rows = database.connection.execute(
            "SELECT code,percent_basis_points,requires_manager "
            "FROM discount_tiers ORDER BY priority"
        ).fetchall()
        assert [(row["code"], row["percent_basis_points"]) for row in rows] == [
            ("bronze", 200),
            ("silver", 400),
            ("gold", 600),
            ("diamond", 1_000),
            ("employee", 1_500),
            ("owner", 2_000),
        ]
        assert rows[-1]["requires_manager"] == 1

        # Uma configuração local é decisão do gerente, não do seed.
        database.connection.execute(
            "UPDATE discount_tiers SET percent_basis_points=750 WHERE code='gold'"
        )
        seed_demo_data(database, config)
        assert database.query_one(
            "SELECT percent_basis_points n FROM discount_tiers WHERE code='gold'"
        )["n"] == 750
        assert database.query_one("SELECT count(*) n FROM discount_tiers")["n"] == 6
    finally:
        database.close()
