"""O roteiro do salão que o PDV em Python e o em C# interpretam igual.

O porte do servidor do salão (C6e) troca ~4.000 linhas de uma vez, e o app
do garçom é reaproveitado sem mudar uma linha: a resposta de cada operação
precisa ser a MESMA. Em vez de um contrato por função, um roteiro: passos em
JSON (criar mesa, abrir comanda, lançar item, transferir, receber...), cada um
com o resultado que o Python obteve. O C# roda os mesmos passos e exige os
mesmos resultados (`SalonContractTests`).

Ids e horários mudam a cada execução, então saem normalizados: o primeiro UUID
visto vira `<id1>`, o segundo `<id2>`, e assim por diante, na ordem em que
aparecem; todo horário ISO vira `<ts>`. Os dois lados normalizam do mesmo jeito.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from pdv.config import AppConfig, PrinterConfig, ScaleConfig
from pdv.data.database import Database
from pdv.domain.errors import PdvError
from pdv.edge.tables import TableService

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
CONTRACT = CONTRACTS / "salon.json"

TENANT = "5a1a0000-0000-4000-8000-000000000001"
STORE = "5a1a0000-0000-4000-8000-000000000002"
DEVICE = "5a1a0000-0000-4000-8000-000000000003"

#: O que o salão precisa existir antes do primeiro passo. Vai no contrato: o
#: C# insere estas mesmas linhas, sem depender do `seed.py` (que é C7a).
SEED: dict[str, list[dict[str, Any]]] = {
    "users": [
        {"id": "5a1a0000-0000-4000-8000-0000000000a1", "tenant_id": TENANT, "name": "João Garçom",
         "login": "joao", "role": "waiter", "updated_at": "2026-09-26T00:00:00.000+00:00"},
        {"id": "5a1a0000-0000-4000-8000-0000000000a2", "tenant_id": TENANT, "name": "Bruno Gerente",
         "login": "bruno", "role": "manager", "can_authorize": 1, "max_discount_percent": "30",
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
    "products": [
        {"id": "5a1a0000-0000-4000-8000-0000000000b1", "tenant_id": TENANT, "store_id": STORE,
         "sku": "CAFE", "name": "Café coado", "pricing_mode": "unit", "price_cents": 700,
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
        {"id": "5a1a0000-0000-4000-8000-0000000000b2", "tenant_id": TENANT, "store_id": STORE,
         "sku": "TORTA", "name": "Torta de limão", "pricing_mode": "weight", "price_cents": 5900,
         "updated_at": "2026-09-26T00:00:00.000+00:00"},
    ],
}

#: Os passos. `save` guarda o `id` do resultado com um nome; `$nome` num
#: argumento é trocado pelo id guardado.
SCRIPT: list[dict[str, Any]] = [
    {"op": "tables.list"},
    {"op": "tables.seed", "args": {"count": 3}},
    {"op": "tables.seed", "args": {"count": 3}},
    {"op": "tables.create", "args": {"label": "  Varanda   1 ", "area": "Varanda", "seats": 2}, "save": "varanda"},
    {"op": "tables.create", "args": {"label": "mesa 1"}},
    {"op": "tables.create", "args": {"label": "   "}},
    {"op": "tables.create", "args": {"label": "Balcão", "area": "", "seats": 500}},
    {"op": "tables.create", "args": {"label": "Balcão", "area": "Bar", "seats": 500}},
    {"op": "tables.create", "args": {"label": "Mesa com um nome comprido demais para caber", "seats": 0}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Varanda 2", "seats": 6}},
    {"op": "tables.update", "args": {"table_id": "$varanda"}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Mesa 2"}},
    {"op": "tables.update", "args": {"table_id": "5a1a0000-0000-4000-8000-00000000dead", "label": "X"}},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": False}},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": False}},
    {"op": "tables.update", "args": {"table_id": "$varanda", "label": "Varanda 3"}},
    {"op": "tables.create", "args": {"label": "Varanda 2"}, "save": "rival"},
    {"op": "tables.set_active", "args": {"table_id": "$varanda", "active": True}},
    {"op": "tables.list", "args": {"include_inactive": True}},
    {"op": "tables.find", "args": {"label": "MESA 3"}},
    {"op": "tables.find", "args": {"label": "Mesa 99"}},
]

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z|[+-]\d{2}:\d{2})?")


class Normalizer:
    """UUID → `<idN>` na ordem de aparição; horário ISO → `<ts>`."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def text(self, value: str) -> str:
        value = _TS.sub("<ts>", value)
        return _UUID.sub(lambda m: self._ids.setdefault(m.group(0), f"<id{len(self._ids) + 1}>"), value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value


def open_database(tmp_path: Path) -> tuple[Database, AppConfig]:
    config = AppConfig(
        tenant_id=TENANT, store_id=STORE, device_id=DEVICE,
        database_path=tmp_path / "pdv.db",
        scale=ScaleConfig(protocol="simulated"),
        printer=PrinterConfig(backend="file", output_dir=tmp_path / "cupons"),
    )
    database = Database(config.database_path)
    database.migrate()
    for table, rows in SEED.items():
        for row in rows:
            columns = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            database.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values())
            )
    database.connection.commit()
    return database, config


def run(tmp_path: Path) -> dict[str, Any]:
    database, config = open_database(tmp_path)
    tables = TableService(database, config)
    saved: dict[str, str] = {}

    def resolve(args: dict[str, Any]) -> dict[str, Any]:
        return {
            k: saved[v[1:]] if isinstance(v, str) and v.startswith("$") else v
            for k, v in args.items()
        }

    handlers: dict[str, Callable[..., Any]] = {
        "tables.list": lambda include_inactive=False: [
            t.to_json() for t in tables.list_tables(include_inactive=include_inactive)
        ],
        "tables.seed": lambda count: tables.seed_default_tables(count),
        "tables.create": lambda **a: tables.create(**a).to_json(),
        "tables.update": lambda table_id, **a: tables.update(table_id, **a).to_json(),
        "tables.set_active": lambda table_id, active: tables.set_active(table_id, active).to_json(),
        "tables.find": lambda label: (lambda t: t.to_json() if t else None)(tables.find_by_label(label)),
    }

    normalizer = Normalizer()
    results = []
    for step in SCRIPT:
        try:
            value = handlers[step["op"]](**resolve(step.get("args", {})))
            if "save" in step:
                saved[step["save"]] = value["id"]
            outcome: dict[str, Any] = {"result": value}
        except PdvError as exc:
            outcome = {"error": str(exc)}
        results.append(normalizer.value(outcome))

    outbox = [
        normalizer.value({
            "entity_table": row["entity_table"],
            "operation": row["operation"],
            "payload": json.loads(row["payload_json"]),
        })
        for row in database.query_all(
            "SELECT entity_table, operation, payload_json FROM sync_outbox ORDER BY seq"
        )
    ]
    database.close()
    return {"results": results, "outbox": outbox}


def document(run_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "descricao": (
            "Roteiro do salão interpretado pelo PDV em Python e pelo em C#. Gerado por "
            "apps/desktop-pdv/tests/salon_script.py; não edite à mão: rode "
            "PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_salon_contract.py."
        ),
        "tenant_id": TENANT,
        "store_id": STORE,
        "device_id": DEVICE,
        "seed": SEED,
        "script": SCRIPT,
        **run_result,
    }
