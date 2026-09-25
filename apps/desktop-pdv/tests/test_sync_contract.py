"""Contrato da sincronização — o que o motor em C# tem de decidir igual.

Os dois PDVs esvaziam a mesma `sync_outbox` contra a mesma nuvem. Uma chave de
idempotência diferente para o mesmo lote é venda aplicada duas vezes; um status
desconhecido tratado como sucesso é venda apagada da fila sem ter chegado; uma
linha de cadastro de outro tenant aceita é login alheio no caixa.

`contracts/sync.json` guarda:

* chaves de idempotência de lotes (`PushBatch.idempotency_key`);
* o veredito de cada status que a nuvem pode mandar (`_parse_status`);
* linhas de cadastro da nuvem e o que o `map_row` grava — ou `null`;
* o backoff por tentativa e os números da fila e do worker.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_sync_contract.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pdv.config import AppConfig
from pdv.sync import engine, outbox, worker
from pdv.sync.protocol import OutboxItem, PushBatch
from pdv.sync.pull_mapping import NOT_APPLIED, PULL_MAPPINGS, map_row
from pdv.sync.transport import _parse_status

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "sync.json"

TENANT = "11111111-1111-1111-1111-111111111111"
STORE = "22222222-2222-2222-2222-222222222222"
CONFIG = AppConfig(tenant_id=TENANT, store_id=STORE, device_id="33333333-3333-3333-3333-333333333333")

BATCHES = [
    ("33333333-3333-3333-3333-333333333333", ["0199aaaa-0000-7000-8000-000000000001"]),
    ("33333333-3333-3333-3333-333333333333", ["0199aaaa-0000-7000-8000-000000000001", "0199aaaa-0000-7000-8000-000000000002"]),
    ("device-ção", ["a", "b", "c"]),
    ("d", []),
]

STATUSES = ["applied", "duplicate", "rejected", "APPLIED", "ok", "", None]

USER = {
    "id": "u-1", "tenant_id": TENANT, "name": "Ana Caixa", "login": "ana", "role": "cashier",
    "pin_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", "max_discount_percent": "10.00",
    "can_authorize": True, "is_active": "t", "updated_at": "2026-09-25T12:00:00.000+00:00",
    "server_seq": "41", "email": "ana@loja.com",
}

PRODUCT = {
    "id": "p-1", "tenant_id": TENANT, "sku": "F1", "barcode": "7890000000011", "name": "Fatia de torta",
    "category": "Doces", "pricing_mode": "unit", "price_cents": "1450", "tare_grams": 0,
    "is_active": 1, "updated_at": "2026-09-25T12:00:00.000+00:00", "server_seq": 700,
    "recipe_yield_grams": 1200,
}

ROWS = [
    ("users", USER),
    ("users", {**USER, "tenant_id": "outro-tenant"}),
    ("users", {**USER, "login": ""}),
    ("users", {**USER, "login": None}),
    ("users", {**USER, "pin_hash": None, "can_authorize": "false", "is_active": 0}),
    ("users", {**USER, "can_authorize": "yes", "is_active": "  TRUE "}),
    ("users", {**USER, "can_authorize": 0.0, "is_active": []}),
    ("users", {**USER, "max_discount_percent": 12.5}),
    ("users", {**USER, "max_discount_percent": 15}),
    ("products", PRODUCT),
    ("products", {**PRODUCT, "price_cents": " 990 "}),
    ("products", {**PRODUCT, "price_cents": "1_000"}),
    ("products", {**PRODUCT, "price_cents": "+5"}),
    ("products", {**PRODUCT, "price_cents": "12.0"}),
    ("products", {**PRODUCT, "price_cents": 12.5}),
    ("products", {**PRODUCT, "price_cents": True}),
    ("products", {**PRODUCT, "barcode": None, "category": None}),
    ("products", {**PRODUCT, "sku": ""}),
    ("products", {k: v for k, v in PRODUCT.items() if k != "price_cents"}),
    ("products", {**PRODUCT, "tare_grams": "abc"}),
    ("products", {**PRODUCT, "store_id": "loja-da-nuvem"}),
]


def _build() -> dict:
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_sync_contract.py. Não edite à mão.",
        "idempotency": [
            {
                "device_id": device,
                "client_uuids": uuids,
                "key": PushBatch(
                    device_id=device, tenant_id=TENANT, store_id=STORE,
                    items=tuple(OutboxItem(seq=i, entity_table="orders", entity_id=u, client_uuid=u,
                                           operation="insert", payload={}) for i, u in enumerate(uuids)),
                ).idempotency_key,
            }
            for device, uuids in BATCHES
        ],
        "statuses": [{"status": s, "verdict": _parse_status(s).value} for s in STATUSES],
        "config": {"tenant_id": TENANT, "store_id": STORE},
        "rows": [{"table": t, "row": row, "mapped": map_row(t, dict(row), CONFIG)} for t, row in ROWS],
        "pull": {
            "pullable": list(engine.PULLABLE_TABLES),
            "mapped": sorted(PULL_MAPPINGS),
            "not_applied": sorted(NOT_APPLIED),
        },
        "outbox": {
            "syncable_tables": sorted(outbox.SYNCABLE_TABLES),
            "max_attempts": outbox.MAX_ATTEMPTS,
            "max_backoff_seconds": outbox.MAX_BACKOFF_SECONDS,
            "backoff_by_attempt": {str(a): min(2**a, outbox.MAX_BACKOFF_SECONDS) for a in range(1, 13)},
        },
        "worker": {
            "idle_seconds": worker.IDLE_INTERVAL_SECONDS,
            "busy_seconds": worker.BUSY_INTERVAL_SECONDS,
            "error_seconds": worker.ERROR_INTERVAL_SECONDS,
            "pull_every_n_cycles": worker.PULL_EVERY_N_CYCLES,
            "clock_skew_warning_ms": engine.CLOCK_SKEW_WARNING_MS,
        },
    }


def test_the_sync_contract_matches_the_implementation() -> None:
    built = json.loads(json.dumps(_build(), ensure_ascii=False))
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == built, (
        "a sincronização mudou: regere o contrato e alinhe o C#"
    )
