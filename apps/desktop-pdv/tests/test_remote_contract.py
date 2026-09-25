"""Contrato do comando remoto — o que o C# tem de assinar, datar e ler igual.

A nuvem (TypeScript) assina, e o terminal confere. Durante a transição, o
terminal pode ser o PDV em Python ou o em C#, e os dois têm de aceitar
exatamente os mesmos comandos. Uma divergência de um byte no texto assinado
aparece como "o painel parou de obedecer" numa loja e não na outra.

O que está aqui, e por que diverge em silêncio entre linguagens:

* **O texto assinado.** Chaves ordenadas por ponto de código (maiúscula antes
  de minúscula), acento cru, `10` inteiro e `10.0` float distintos, array na
  ordem recebida e o separador `\\x1f`.
* **A janela de validade.** São 12 h para trás e 5 min de tolerância para a
  frente. Data sem fuso usa o fuso de referência, e data ilegível conta como
  vencida.
* **O percentual.** É o `Decimal(str(valor))` do Python: `"1e1"` vale 10,
  `True` não vale, NaN e infinito não valem. Também o `_plain`, que é como o
  número volta ao painel na mensagem de recusa.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_remote_contract.py``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pdv.remote.commands import CommandRefused, _percent, _plain
from pdv.remote.protocol import (
    CLOCK_SKEW_TOLERANCE,
    MAX_COMMAND_AGE,
    _material,
    canonical_payload,
    is_fresh,
    sign_command,
)

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "remote-commands.json"

SECRET = bytes(range(32))
NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

PAYLOADS: list[dict] = [
    {},
    {"order_id": "abc", "percent": "12.5", "reason": "Atraso na cozinha"},
    {"order_id": "abc", "percent": 10, "reason": "Cliente fiel"},
    {"percent": 10.0},
    {"percent": 12.5, "z": None, "t": True, "f": False},
    {"z": 1, "a": 2, "m": "ç ã é", "vazio": ""},
    {"aninhado": {"b": 2, "a": [3, 1, 2]}},
    {"B": 1, "a": 2, "_": 3, "Ç": 4},
    {"emoji": "bolo 🎂", "aspas": "a\"b\\c", "linha": "x\ny\tz"},
    {"order_item_id": "i-1", "order_id": "o-1", "reason": "Pedido trocado"},
]

ISSUED_AT: list[str] = [
    "2026-09-25T12:00:00+00:00",
    "2026-09-25T12:00:00.000Z",
    "2026-09-25T00:00:00+00:00",
    "2026-09-24T23:59:59+00:00",
    "2026-09-25T12:05:00+00:00",
    "2026-09-25T12:05:01+00:00",
    "2026-09-25T09:00:00-03:00",
    "2026-09-24T20:59:59-03:00",
    "2026-09-25T11:00:00",
    "2026-09-25 11:00:00+00:00",
    "2026-09-25T11:00:00.123456+00:00",
    "2026-09-25",
    "2026-09-26",
    "ontem",
    "",
    "2026-13-01T00:00:00+00:00",
]

PERCENTS: list[object] = [
    "12.5", 12.5, 10, 10.0, "10.50", "1e1", " 7 ", "+5", "0.01", "100", 100, "100.0",
    0, "0", -1, 100.01, "101", "abc", "12,5", "NaN", "Infinity", "-Infinity", True, None, [], {},
]


def _percent_case(value: object) -> dict:
    try:
        parsed = _percent({"percent": value}, "percent")
    except CommandRefused:
        return {"value": value, "error": True}
    return {"value": value, "plain": _plain(parsed)}


def _build() -> dict:
    signatures = []
    for index, payload in enumerate(PAYLOADS):
        command = {
            "command_uuid": f"cmd-{index}",
            "device_id": "33333333-3333-3333-3333-333333333333",
            "kind": "cancel_item" if "order_item_id" in payload else "apply_discount",
            "payload": payload,
            "issued_at": "2026-09-19T12:00:00+00:00",
        }
        signatures.append({
            **command,
            "canonical_payload": canonical_payload(payload),
            "material": _material(**command).decode("utf-8"),
            "signature": sign_command(secret=SECRET, **command),
        })
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_remote_contract.py. Não edite à mão.",
        "secret_hex": SECRET.hex(),
        "max_age_seconds": int(MAX_COMMAND_AGE.total_seconds()),
        "skew_seconds": int(CLOCK_SKEW_TOLERANCE.total_seconds()),
        "now": NOW.isoformat(),
        "signatures": signatures,
        "freshness": [{"issued_at": value, "fresh": is_fresh(value, now=NOW)} for value in ISSUED_AT],
        "percents": [_percent_case(value) for value in PERCENTS],
    }


def test_the_remote_contract_matches_the_implementation() -> None:
    built = _build()
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == built, (
        "a assinatura, a validade ou a leitura do comando remoto mudou: regere o contrato e alinhe o C#"
    )


def test_the_contract_really_exercises_the_traps() -> None:
    built = _build()
    fresh = {case["issued_at"]: case["fresh"] for case in built["freshness"]}
    assert fresh["2026-09-25T00:00:00+00:00"] and not fresh["2026-09-24T23:59:59+00:00"]  # 12 h exatas
    assert fresh["2026-09-25T12:05:00+00:00"] and not fresh["2026-09-25T12:05:01+00:00"]  # 5 min de folga
    assert not fresh["ontem"] and not fresh[""]
    canonical = {json.dumps(s["payload"], sort_keys=True): s["canonical_payload"] for s in built["signatures"]}
    assert canonical[json.dumps({"percent": 10.0}, sort_keys=True)] == '{"percent":10.0}'
    assert '"B":1,"_":3,"a":2' in canonical[json.dumps({"B": 1, "a": 2, "_": 3, "Ç": 4}, sort_keys=True)]
    percents = {json.dumps(p["value"]): p for p in built["percents"]}
    assert percents['"1e1"']["plain"] == "10"
    assert percents["true"].get("error") and percents['"NaN"'].get("error")
