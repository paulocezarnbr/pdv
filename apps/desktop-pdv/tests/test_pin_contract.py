"""Contrato do PIN — o que o login do PDV em C# tem de aceitar e recusar.

Os dois PDVs leem a mesma tabela `users`. Um hash de PIN gravado pelo Python
(ou baixado da nuvem) que o C# não verifica é um caixa em que ninguém entra. Um
PIN que o Python recusa e o C# aceita é a política de senha furada pela porta
nova.

`contracts/pin-hashes.json` guarda:

* hashes Argon2id gerados pelo `hash_pin` real, com o PIN certo e PINs errados
  (o sal é aleatório, então o arquivo congela os hashes; este teste confere que
  continuam verificando no Python);
* a política: PINs e o veredito do `validate_pin` — aceito, ou a mensagem;
* os números do freio de tentativas, que os dois aplicam sobre a mesma
  `auth_throttle`.

A volta (hash gerado pelo C#, verificado pelo Python) é feita por
`apps/pdv-net/crosscheck.py` no CI.

Para regerar: ``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_pin_contract.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pdv.services import authorization as auth

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "pin-hashes.json"

PINS = ["480362", "9051774", "205813970462"]

POLICY = [
    "480362", "123456", "654321", "000000", "111111", "12345", "1234567890123",
    "12a456", " 480362 ", "135790", "987654", "909090", "121212", "246802",
    "890123", "010101", "7777777", "5820194",
]


def _verdict(pin: str) -> dict:
    try:
        return {"pin": pin, "accepted": True, "normalized": auth.validate_pin(pin)}
    except auth.WeakPinError as error:
        return {"pin": pin, "accepted": False, "message": str(error)}


def _build(previous: dict | None) -> dict:
    hashes = (previous or {}).get("hashes") or [
        {"pin": pin, "hash": auth.hash_pin(pin), "wrong": [pin[:-1] + str((int(pin[-1]) + 1) % 10), ""]}
        for pin in PINS
    ]
    return {
        "comment": "Gerado por apps/desktop-pdv/tests/test_pin_contract.py. Não edite à mão.",
        "hashes": hashes,
        "dummy_hash": auth._DUMMY_HASH,
        "policy": [_verdict(pin) for pin in POLICY],
        "throttle": {
            "max_attempts": auth.MAX_ATTEMPTS,
            "max_global_attempts": auth.MAX_GLOBAL_ATTEMPTS,
            "lockout_base_seconds": auth.LOCKOUT_BASE_SECONDS,
            "lockout_max_seconds": auth.LOCKOUT_MAX_SECONDS,
            "max_exponent": auth._MAX_EXPONENT,
            "failure_window_seconds": int(auth.FAILURE_WINDOW.total_seconds()),
            "global_scope": auth._GLOBAL,
        },
    }


def test_the_pin_contract_matches_the_implementation() -> None:
    previous = json.loads(CONTRACT.read_text(encoding="utf-8")) if CONTRACT.exists() else None
    built = _build(previous)
    if os.getenv("PDV_UPDATE_CONTRACT") == "1":
        CONTRACT.write_text(json.dumps(built, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        previous = built
    assert previous is not None, "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    assert previous == built, "a política ou o freio mudou: regere o contrato e alinhe o C#"


def test_the_frozen_hashes_still_verify_in_python() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for case in contract["hashes"]:
        assert auth._verify(case["hash"], case["pin"]), case["pin"]
        for wrong in case["wrong"]:
            assert not auth._verify(case["hash"], wrong), wrong
