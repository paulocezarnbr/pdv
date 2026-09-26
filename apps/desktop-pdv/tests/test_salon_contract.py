"""O roteiro do salão (`contracts/salon.json`) é o que o PDV em Python faz hoje.

Mudou uma resposta do salão, este teste falha até o contrato ser regerado — e
aí o C# (`SalonContractTests`) exige a resposta nova. Para regerar:
``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_salon_contract.py``.
"""

from __future__ import annotations

import json
import os

import salon_script


def test_the_salon_contract_is_what_the_python_pdv_answers(tmp_path) -> None:  # noqa: ANN001
    document = salon_script.document(salon_script.run(tmp_path))
    if os.environ.get("PDV_UPDATE_CONTRACT") == "1":
        salon_script.CONTRACT.write_text(
            json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )

    assert salon_script.CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    saved = json.loads(salon_script.CONTRACT.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(document, ensure_ascii=False))


def test_every_step_either_answers_or_refuses(tmp_path) -> None:  # noqa: ANN001
    """Um passo que não deu nem resultado nem recusa é roteiro quebrado."""
    for outcome in salon_script.run(tmp_path)["results"]:
        assert ("result" in outcome) != ("error" in outcome)
