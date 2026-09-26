"""As rotas do salão (`contracts/salon-http.json`) são o que o FastAPI responde hoje.

Mudou uma resposta, este teste falha até o contrato ser regerado — e aí o
servidor em C# (`SalonHttpContractTests`) exige a resposta nova. Para regerar:
``PDV_UPDATE_CONTRACT=1 python -m pytest tests/test_salon_http_contract.py``.
"""

from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("fastapi.testclient")

import salon_http_script  # noqa: E402


def test_the_route_contract_is_what_the_python_server_answers(tmp_path) -> None:  # noqa: ANN001
    document = salon_http_script.document(salon_http_script.run(tmp_path))
    if os.environ.get("PDV_UPDATE_CONTRACT") == "1":
        salon_http_script.CONTRACT.write_text(
            json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )

    assert salon_http_script.CONTRACT.exists(), "rode com PDV_UPDATE_CONTRACT=1 para gerar o contrato"
    saved = json.loads(salon_http_script.CONTRACT.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(document, ensure_ascii=False))


def test_no_route_answers_500(tmp_path) -> None:  # noqa: ANN001
    """500 é o servidor sem saber o que dizer; o app do garçom mostra "Erro 500"."""
    for outcome in salon_http_script.run(tmp_path)["results"]:
        assert outcome.get("status", 200) < 500, outcome
