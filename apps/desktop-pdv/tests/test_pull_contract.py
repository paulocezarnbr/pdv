"""O pull de cadastros contra o schema REAL da nuvem.

O pull nunca aplicou uma linha: o caixa gravava todas as colunas que a nuvem
mandasse, e as duas pontas nunca tiveram o mesmo schema (`users.server_seq`,
`products` sem `store_id`). Nenhum teste pegou porque nenhum teste mandava uma
linha no formato da nuvem — os dublês mandavam linhas no formato do caixa.

Por isso este arquivo lê as migrations da nuvem (`apps/cloud-api/migrations`)
e monta as linhas com as colunas que ela realmente tem, como o teste de
contrato do HMAC faz com a assinatura. Coluna nova na nuvem entra aqui sozinha:
se ela quebrar o caixa, quebra este teste antes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.sync.engine import SyncEngine
from pdv.sync.protocol import PullResponse, PushResponse
from pdv.sync.pull_mapping import NOT_APPLIED, PULL_MAPPINGS, map_row

MIGRATIONS = Path(__file__).resolve().parents[2] / "cloud-api" / "migrations"
TENANT = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER = "bbbbbbbb-0000-0000-0000-000000000001"
_SKIP = ("CONSTRAINT", "UNIQUE", "PRIMARY", "CHECK", "FOREIGN", "--", ")")


def cloud_columns(table: str) -> set[str]:
    """Colunas da tabela na nuvem: o CREATE TABLE mais os ADD COLUMN posteriores."""
    columns: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        create = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", text, re.S
        )
        if create:
            for line in create.group(1).splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(_SKIP):
                    continue
                columns.add(stripped.split()[0].strip(","))
        for match in re.finditer(
            rf"ALTER TABLE {table}\s+(.*?);", text, re.S
        ):
            columns.update(re.findall(r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", match.group(1)))
    return columns


def cloud_row(table: str, **values: object) -> dict[str, object]:
    """Uma linha com TODAS as colunas da nuvem, no formato do JSON dela.

    BIGINT chega como texto (o driver faz isso para não perder precisão),
    booleano como booleano, NUMERIC como texto.
    """
    row: dict[str, object] = {name: None for name in cloud_columns(table)}
    row.update({"server_seq": "4711", "updated_at": "2026-09-24T10:00:00.000Z"})
    row.update(values)
    return {key: value for key, value in row.items() if key in cloud_columns(table)}


@pytest.fixture()
def terminal(tmp_path: Path):  # noqa: ANN201
    config = AppConfig(
        tenant_id=TENANT,
        store_id="aaaaaaaa-0000-0000-0000-0000000000ff",
        device_id="aaaaaaaa-0000-0000-0000-0000000000dd",
        database_path=tmp_path / "pdv.db",
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "c"),
    )
    database = Database(config.database_path)
    database.migrate()
    return database, config


class _Cloud:
    def __init__(self, tables: dict[str, tuple[dict[str, object], ...]]) -> None:
        self.tables = tables
        self.asked: list[str] = []

    def push(self, batch):  # noqa: ANN001, ANN201
        return PushResponse(acks=())

    def pull(self, request):  # noqa: ANN001, ANN201
        self.asked.append(request.entity_table)
        rows = self.tables.get(request.entity_table, ())
        return PullResponse(
            entity_table=request.entity_table,
            rows=rows if request.since_server_seq == 0 else (),
            last_server_seq=4711,
        )


def test_the_cloud_schema_is_readable_from_here() -> None:
    """Sem as migrations o contrato não prova nada — e o teste não pode pular."""
    assert {"server_seq", "pin_hash", "login"} <= cloud_columns("users")
    assert {"sku", "price_cents"} <= cloud_columns("products")


def test_users_from_the_cloud_become_logins_here(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    cloud = _Cloud({
        "users": (
            cloud_row(
                "users", id="11111111-0000-0000-0000-000000000001", tenant_id=TENANT,
                name="Carla Caixa", login="carla", role="cashier", pin_hash="$argon2id$x",
                can_authorize=False, max_discount_percent="5.00", is_active=True,
            ),
        ),
    })

    applied = SyncEngine(database, cloud, config).pull_once()

    assert applied == 1
    row = database.query_one("SELECT * FROM users WHERE login = 'carla'")
    assert row["tenant_id"] == TENANT
    assert row["pin_hash"] == "$argon2id$x"
    assert row["is_active"] == 1
    assert row["can_authorize"] == 0
    assert row["max_discount_percent"] == "5.00"


def test_products_from_the_cloud_land_in_this_store(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    cloud = _Cloud({
        "products": (
            cloud_row(
                "products", id="22222222-0000-0000-0000-000000000001", tenant_id=TENANT,
                sku="PAO-QUEIJO", name="Pão de queijo", pricing_mode="unit",
                price_cents="650", tare_grams=0, is_active=True, barcode=None,
            ),
        ),
    })

    SyncEngine(database, cloud, config).pull_once()

    row = database.query_one("SELECT * FROM products WHERE sku = 'PAO-QUEIJO'")
    assert row["store_id"] == config.store_id
    assert row["price_cents"] == 650
    assert row["server_seq"] == 4711


def test_pulling_again_updates_instead_of_duplicating(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    product = dict(
        id="22222222-0000-0000-0000-000000000001", tenant_id=TENANT, sku="CAFE",
        name="Café", pricing_mode="unit", price_cents="500", is_active=True,
    )
    engine = SyncEngine(database, _Cloud({"products": (cloud_row("products", **product),)}), config)
    engine.pull_once()

    product["price_cents"] = "550"
    engine._apply_pulled_rows("products", (cloud_row("products", **product),))

    rows = database.query_all("SELECT price_cents FROM products WHERE sku = 'CAFE'")
    assert [r["price_cents"] for r in rows] == [550]


def test_a_row_from_another_tenant_never_becomes_a_login(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    cloud = _Cloud({
        "users": (
            cloud_row("users", id="u-x", tenant_id=OTHER, name="Intrusa",
                      login="intrusa", role="owner", pin_hash="x", is_active=True),
        ),
    })

    SyncEngine(database, cloud, config).pull_once()

    assert database.query_one("SELECT 1 FROM users WHERE login = 'intrusa'") is None


def test_a_new_cloud_column_does_not_break_the_counter(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    row = cloud_row("users", id="u1", tenant_id=TENANT, name="Ana", login="ana2",
                    role="cashier", pin_hash="x", is_active=True)
    row["coluna_que_ainda_nao_existe"] = "qualquer coisa"

    applied = SyncEngine(database, _Cloud({"users": (row,)}), config).pull_once()

    assert applied == 1


def test_an_incomplete_row_is_dropped_not_half_written(terminal) -> None:  # noqa: ANN001
    database, config = terminal
    row = cloud_row("users", id="u1", tenant_id=TENANT, name=None, login="semnome",
                    role="cashier")

    SyncEngine(database, _Cloud({"users": (row,)}), config).pull_once()

    assert database.query_one("SELECT 1 FROM users WHERE login = 'semnome'") is None


def test_tables_with_a_different_model_are_not_even_requested(terminal) -> None:  # noqa: ANN001
    """Pedir e não aplicar avançaria o cursor e perderia as linhas para sempre."""
    database, config = terminal
    cloud = _Cloud({})

    SyncEngine(database, cloud, config).pull_once()

    assert not set(cloud.asked) & set(NOT_APPLIED)
    assert set(cloud.asked) == set(PULL_MAPPINGS)


def test_one_table_failing_does_not_stop_the_others(terminal, monkeypatch) -> None:  # noqa: ANN001
    import sqlite3

    database, config = terminal
    engine = SyncEngine(database, _Cloud({
        "products": (cloud_row("products", id="p1", tenant_id=TENANT, sku="X",
                               name="X", pricing_mode="unit", price_cents="1"),),
        "users": (cloud_row("users", id="u1", tenant_id=TENANT, name="Ana",
                            login="ana3", role="cashier", pin_hash="x"),),
    }), config)
    original = engine._apply_pulled_rows

    def flaky(table, rows):  # noqa: ANN001, ANN202
        if table == "products":
            raise sqlite3.OperationalError("disk I/O error")
        return original(table, rows)

    monkeypatch.setattr(engine, "_apply_pulled_rows", flaky)

    engine.pull_once()

    assert database.query_one("SELECT 1 FROM users WHERE login = 'ana3'") is not None


def test_every_mapped_column_exists_on_both_sides(terminal) -> None:  # noqa: ANN001
    """O mapeamento não pode prometer uma coluna que um dos lados não tem."""
    database, _config = terminal
    for table, mapping in PULL_MAPPINGS.items():
        local = {r["name"] for r in database.query_all(f"PRAGMA table_info({table})")}
        remote = cloud_columns(table)
        for column in mapping.columns:
            assert column in local, f"{table}.{column} não existe no caixa"
            assert column in remote or column == "category", (
                f"{table}.{column} não existe na nuvem"
            )


def test_map_row_ignores_what_it_does_not_know() -> None:
    config = AppConfig(tenant_id=TENANT, store_id="s", device_id="d")
    mapped = map_row("users", {"id": "u", "tenant_id": TENANT, "name": "A", "login": "a",
                               "role": "cashier", "updated_at": "x", "rogue); DROP": 1}, config)

    assert mapped is not None
    assert "rogue); DROP" not in mapped
