"""Contrato da cadeia de auditoria — o que o PDV em C# tem de reproduzir.

O PDV está sendo portado para C# por etapas (ver `docs/port_csharp.md`), e
durante a transição os dois gravam e verificam o **mesmo** `audit_ledger`. Um
JSON canônico que difira num único byte — uma chave em outra ordem, um "ç"
escapado, um `1e-05` escrito `1E-05` — muda o HMAC, e a verificação da cadeia
acusa adulteração onde não houve nenhuma: o caixa não abre.

Este arquivo gera `contracts/audit-chain.json` a partir da implementação real
(`pdv.services.audit`). Os testes do C# (`apps/pdv-net`) leem o mesmo arquivo e
exigem igualdade byte a byte. Mudou o formato aqui, o C# reprova até ser
alinhado.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_audit_contract.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pdv.domain.models import GENESIS_HASH
from pdv.services.audit import canonical_payload, compute_hash

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "audit-chain.json"

SECRET = bytes(range(32))
CREATED_AT = "2026-09-25T03:49:25.134+00:00"

PAYLOADS: dict[str, dict] = {
    "simples": {"total_cents": 1250, "order_id": "01a0d6ae-a60e-77ed-a0da-26abac491cec", "items": 3},
    "acentos_e_escapes": {
        "produto": "Pão de Queijo — edição ção",
        "obs": 'linha1\nlinha2\t"aspas" \\ barra / fim\r',
        "controle": "\u0001\u001f\u007f\b\f",
        "emoji": "🍰 bolo",
    },
    "aninhado_e_ordem": {
        "b": {"z": 1, "a": [3, 2, {"y": None, "x": True}]},
        "a": False,
        "A": 0,
        "_": "",
        "aa": [],
        "ab": {},
    },
    "numeros": {
        "negativo": -5,
        "grande": 9007199254740993,
        "zero": 0,
        "meio": 1.5,
        "decimo": 0.1,
        "pequeno": 1e-05,
        "limite_fixo": 0.0001,
        "grande_float": 1e16,
        "quase_limite": 1e15,
        "inteiro_float": 2.0,
        "fracao": 123456.789,
        "negativo_float": -0.0001,
        "precisao": 1.2345678901234567e19,
        "zero_negativo": -0.0,
    },
    "vazio": {},
}


def _build() -> dict:
    cases = []
    for name, payload in PAYLOADS.items():
        canonical = canonical_payload(payload)
        cases.append(
            {
                "name": name,
                "payload": payload,
                "canonical": canonical,
                "hash": compute_hash(
                    secret=SECRET,
                    prev_hash=GENESIS_HASH,
                    seq=1,
                    event_type="sale_closed",
                    payload_json=canonical,
                    created_at=CREATED_AT,
                ),
            }
        )

    chain = []
    prev = GENESIS_HASH
    for seq, (event, payload) in enumerate(
        [
            ("item_registered", {"product_id": "p1", "qty": 2, "price_cents": 450}),
            ("discount_applied", {"percent": 10, "reason": "Cliente fidelidade"}),
            ("sale_closed", {"total_cents": 810, "payments": [{"method": "debit", "nsu": "000123"}]}),
        ],
        start=1,
    ):
        payload_json = canonical_payload(payload)
        digest = compute_hash(
            secret=SECRET,
            prev_hash=prev,
            seq=seq,
            event_type=event,
            payload_json=payload_json,
            created_at=CREATED_AT,
        )
        chain.append(
            {
                "seq": seq,
                "event_type": event,
                "payload_json": payload_json,
                "prev_hash": prev,
                "hash": digest,
                "created_at": CREATED_AT,
            }
        )
        prev = digest

    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_audit_contract.py. Não edite à mão.",
        "genesis_hash": GENESIS_HASH,
        "secret_hex": SECRET.hex(),
        "created_at": CREATED_AT,
        "event_type": "sale_closed",
        "cases": cases,
        "chain": chain,
    }


def test_the_audit_contract_matches_the_implementation() -> None:
    built = _build()
    text = json.dumps(built, ensure_ascii=False, indent=2) + "\n"
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(text, encoding="utf-8")
    assert CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == json.loads(text), (
        "o formato da auditoria mudou: regere o contrato e alinhe o C# (apps/pdv-net)"
    )


def test_the_contract_payloads_survive_a_json_round_trip() -> None:
    """O C# recebe o payload pelo arquivo: ele tem de chegar igual ao Python."""
    for case in _build()["cases"]:
        again = json.loads(json.dumps(case["payload"]))
        assert canonical_payload(again) == case["canonical"], case["name"]
